"""One-off sanity check for dispatch_gate.py -- plain asserts, no
pytest, matches this project's existing test_*.py convention.

check() itself is a pure function (no DB access by design -- see the
module's own docstring on the deliberate deviation from Kimi's
DB-lookup sketch), so most cases here need no database at all. Only
log_intervention()'s test touches wardence.db, scoped with a throwaway
episode_id so it never collides with real data (same pattern
test_misdispatch_guard.py already uses -- namespacing, not a separate
test DB file, matches this project's actual established convention).

Case 8 below (patch_cpu_limit's same-tool magnitude check) calls
constraint_checks.check_safe(), which does a REAL kubectl read of
user's live CPU limit against this lab's live cluster -- this is an
integration check, not a pure unit test, and requires the real k3s
cluster to be up with `user` at its normal state (needs
injector.FAULT_CONFIG["cpu-throttling"]'s real target/namespace to
resolve). Run this after confirming the cluster is healthy, not as a
pre-cluster smoke test. Every other case here is DB-free and
cluster-free by construction (patch_memory_limit's bound comes from
tool_output, not a live read).
"""
import sqlite3

from dispatch_gate import DB_PATH, check, ensure_gate_tables, log_intervention

TEST_EPISODE_ID = "test-dispatch-gate-episode"

# Fake ACTION_MAP, same shape as p3_agent.py's real one --
# class -> (fault_class, tool_name, kwargs_fn(target, namespace)).
FAKE_ACTION_MAP = {
    "under-provisioned-replicas": (
        "under-provisioned-replicas", "scale_deployment",
        lambda target, namespace: {"name": target, "namespace": namespace, "replicas": 4},
    ),
    "oom": (
        "oom", "patch_memory_limit",
        lambda target, namespace: {"name": target, "namespace": namespace, "container": target, "limit": "400Mi"},
    ),
    "crash-loop": (
        "crash-loop", "restart_deployment",
        lambda target, namespace: {"name": target, "namespace": namespace},
    ),
    "bad-rollout": (
        "bad-rollout", "rollback_deployment",
        lambda target, namespace: {"name": target, "namespace": namespace},
    ),
    # Deliberately no entry for "session-cart-failure" -- covers the
    # "actual_class has no auto-fix mapping" case.
}

# --- Case 1: predicted == actual -- never fires, trivial early return ---
r1 = check(FAKE_ACTION_MAP, "oom", "oom", "patch_memory_limit",
           {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"},
           "catalogue", "sock-shop", {"peak_memory_mib": 180})
print("case 1 (predicted==actual):", r1)
assert r1["substituted"] is False

# --- Case 2: cross-tool misdispatch -- must fire ---
# Agent predicted under-provisioned-replicas (proposes scale_deployment),
# real fault is oom (needs patch_memory_limit). Different tools.
r2 = check(FAKE_ACTION_MAP, "under-provisioned-replicas", "oom", "scale_deployment",
           {"name": "catalogue", "namespace": "sock-shop", "replicas": 4},
           "catalogue", "sock-shop", {"peak_memory_mib": 180})
print("case 2 (cross-tool):", r2)
assert r2["substituted"] is True
assert r2["actual_tool"] == "patch_memory_limit"
assert r2["actual_params"]["limit"] == "400Mi"

# --- Case 3: same tool, NOT magnitude-sensitive -- never fires ---
# Both predicted and actual happen to map to restart_deployment/
# rollback_deployment-shaped tools with no magnitude parameter to abuse.
# Simulated by predicting crash-loop (restart_deployment) against an
# actual class whose deterministic tool is also restart_deployment --
# reuse crash-loop as both predicted and a synthetic "actual" via a
# second entry sharing the same tool, since FAKE_ACTION_MAP's real
# classes don't have two restart_deployment entries; test check()'s
# tool-comparison logic directly instead.
r3 = check(
    {**FAKE_ACTION_MAP, "fake-restart-twin": ("fake-restart-twin", "restart_deployment",
                                               lambda t, n: {"name": t, "namespace": n})},
    "crash-loop", "fake-restart-twin", "restart_deployment",
    {"name": "carts", "namespace": "sock-shop"}, "carts", "sock-shop", None,
)
print("case 3 (same tool, no magnitude):", r3)
assert r3["substituted"] is False

# --- Case 4: same tool, magnitude-sensitive, SAFE proposed value ---
# Predicted and actual are both "oom" in tool terms (contrived: agent
# somehow proposed patch_memory_limit for a different auto-fix class
# that also happens to map to patch_memory_limit) with a value that
# passes constraint-satisfaction (above real peak, within cap).
r4 = check(
    {**FAKE_ACTION_MAP, "fake-oom-twin": ("fake-oom-twin", "patch_memory_limit",
                                           lambda t, n: {"name": t, "namespace": n, "container": t, "limit": "400Mi"})},
    "oom", "fake-oom-twin", "patch_memory_limit",
    {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "450Mi"},
    "catalogue", "sock-shop", {"peak_memory_mib": 180},
)
print("case 4 (same tool, safe magnitude):", r4)
assert r4["substituted"] is False

# --- Case 5: same tool, magnitude-sensitive, UNSAFE proposed value ---
# Proposed limit (150Mi) is below the real peak usage (180Mi) -- unsafe,
# must fire even though the tool matches.
r5 = check(
    {**FAKE_ACTION_MAP, "fake-oom-twin": ("fake-oom-twin", "patch_memory_limit",
                                           lambda t, n: {"name": t, "namespace": n, "container": t, "limit": "400Mi"})},
    "oom", "fake-oom-twin", "patch_memory_limit",
    {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "150Mi"},
    "catalogue", "sock-shop", {"peak_memory_mib": 180},
)
print("case 5 (same tool, UNSAFE magnitude):", r5)
assert r5["substituted"] is True
assert r5["actual_tool"] == "patch_memory_limit"

# --- Case 6: agent said report-only (no proposed action at all) ---
# Not check()'s job to guard this -- p3_agent.py's /act simply never
# calls check() when there's no proposal/dispatch happening. Documented
# here as a reminder, not a real assertion against check() itself.
print("case 6 (report-only): gate is never called in this path -- "
      "enforced by /act's own control flow, not check()'s responsibility.")

# --- Case 7: actual_class has no auto-fix mapping at all ---
r7 = check(FAKE_ACTION_MAP, "oom", "session-cart-failure", "patch_memory_limit",
           {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"},
           "catalogue", "sock-shop", {"peak_memory_mib": 180})
print("case 7 (actual has no mapping):", r7)
assert r7["substituted"] is False

# --- Case 8: same tool, magnitude-sensitive (CPU), live-cluster check ---
# Real integration case, needs the actual cluster: check_safe() resolves
# the live target via injector.FAULT_CONFIG[actual_class], so
# actual_class MUST be a real class key here ("cpu-throttling", real
# target="user") -- NOT a synthetic name, unlike the memory cases above
# which only need tool_output (no FAULT_CONFIG lookup). An absurdly
# high proposed value (4000m, at the tool_call_validator cap) should
# always be unsafe against any plausible live limit (300-600m range,
# per this project's own real history) -- fires regardless of the
# live limit's exact current value.
r8 = check(
    {**FAKE_ACTION_MAP, "cpu-throttling": ("cpu-throttling", "patch_cpu_limit",
                                            lambda t, n: {"name": t, "namespace": n, "container": t, "limit": "600m"})},
    "crash-loop", "cpu-throttling", "patch_cpu_limit",
    {"name": "user", "namespace": "sock-shop", "container": "user", "limit": "4000m"},
    "user", "sock-shop", None,
)
print("case 8 (same tool, UNSAFE CPU magnitude, live cluster read):", r8)
assert r8["substituted"] is True
assert r8["actual_tool"] == "patch_cpu_limit"

# --- Case 9: log_intervention writes a real, scoped, cleaned-up row ---
conn = sqlite3.connect(DB_PATH)
ensure_gate_tables(conn)
conn.execute("DELETE FROM gate_intervention_log WHERE episode_id = ?", (TEST_EPISODE_ID,))
conn.commit()

log_intervention(conn, TEST_EPISODE_ID, "under-provisioned-replicas", "oom",
                  "scale_deployment", "patch_memory_limit", "test reason")
row = conn.execute(
    "SELECT predicted_class, actual_class, proposed_tool, substituted_tool, reason "
    "FROM gate_intervention_log WHERE episode_id = ?", (TEST_EPISODE_ID,)
).fetchone()
print("case 9 (log_intervention row):", row)
assert row == ("under-provisioned-replicas", "oom", "scale_deployment", "patch_memory_limit", "test reason")

conn.execute("DELETE FROM gate_intervention_log WHERE episode_id = ?", (TEST_EPISODE_ID,))
conn.commit()
conn.close()

print("\nALL ASSERTIONS PASSED (cases 1-9, including one live-cluster "
      "read in case 8 -- scale_deployment's own live-replica-count path "
      "isn't separately covered here, same kubectl-read pattern as "
      "case 8's CPU check, add if it ever needs its own regression test)")
