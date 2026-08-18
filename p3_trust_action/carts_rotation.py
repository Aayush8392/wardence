"""
crash-loop / carts warm-standby: the Service-selector flip mechanism.

Locked design: wardence_crash_loop_warm_standby_LOCKED_SPEC.md.

Deliberately NOT part of actions.py / ALLOWED_ACTIONS, and never
called from the agent's own ReAct loop or exposed as a tool it can
invoke. This is cosmetic demo-visibility plumbing, not a scored fix --
the trust ladder (Dimension A/B/C) must only ever judge
`restart_deployment`'s real correctness. Keeping this in its own
module, never registered as an action, is what guarantees a flip
failure/success can never leak into what gets scored (per the real
risk both Kimi review 49 and the Qwen review flagged: bundling cosmetic
and scored outcomes together lets one corrupt the other).

Reuses actions.py's existing SA-authenticated CoreV1Api client
(_core_v1()) rather than a separate ServiceAccount -- the real
isolation guarantee here comes from this module never being wired into
the agent's tool-calling surface, not from a second identity. RBAC:
see manifests/rbac.yaml's `services` grant, scoped via `resourceNames`
to the carts Service only (get + patch, no list -- list can't be
meaningfully resourceName-scoped in Kubernetes RBAC).

Model A, locked: the fault ALWAYS targets the literal `carts`
Deployment. `carts-warm` is a permanent, never-faulted standby
(created once via create_carts_warm_standby.py) that exists purely to
receive real traffic while `carts` recovers. This module only ever
flips the Service selector between the two literal values `carts` and
`carts-warm` -- it never restarts, scales, or otherwise mutates either
Deployment. Restoration (flip_to_carts) must ONLY ever patch the
selector, never re-issue a restart on carts (an extra restart would
bump carts's real restart count and could false-flap the next
durability window).
"""

from actions import _core_v1

NAMESPACE = "sock-shop"
SERVICE_NAME = "carts"
CARTS_LABEL = "carts"
CARTS_WARM_LABEL = "carts-warm"


def get_active_label() -> str | None:
    """Reads the carts Service's CURRENT real selector value -- this
    IS the rotation state, derived live from the cluster, never
    persisted anywhere (per the Qwen review's explicit recommendation:
    the cluster already holds this fact for free, a stored flag would
    just be a second source of truth that can drift from it).

    Fails closed -- returns None (never "known to be carts" or "known
    to be carts-warm") on ANY real-world failure: an expired/missing SA
    token (_core_v1() itself can raise RuntimeError/FileNotFoundError,
    not just a Kubernetes-API-level error), a transient API hiccup, an
    RBAC denial. This function is called on every /trigger/status poll
    (frequent, public, feeds unrelated widgets for every OTHER fault
    class too) and inside the live /trigger/inject request path -- a
    real exception here must never take either endpoint down over a
    crash-loop-specific concern."""
    try:
        core = _core_v1()
        svc = core.read_namespaced_service(SERVICE_NAME, NAMESPACE)
        selector = svc.spec.selector or {}
        return selector.get("name")
    except Exception as e:
        print(f"  carts_rotation.get_active_label(): real failure, treating as unknown "
              f"(fail-closed): {e}")
        return None


def _is_pod_ready(label_value: str) -> bool:
    """Live API check (not Prometheus -- real scrape lag doctrine
    already established elsewhere in this project) that a pod with
    the given `name` label is genuinely Running AND Ready, not just
    Running. Returns False on ANY failure (no pod found, a Kubernetes-
    API-level error, an expired/missing SA token, a transient network
    issue) rather than raising -- callers treat "not confirmed ready"
    as the safe default in every case. Broad `except Exception`,
    deliberately -- not just `client.ApiException` -- since _core_v1()
    itself can raise a plain RuntimeError (expired token) or
    FileNotFoundError (missing sa_token.txt), neither of which is a
    Kubernetes API error, and both are just as real a reason to fail
    closed here."""
    try:
        core = _core_v1()
        pods = core.list_namespaced_pod(
            NAMESPACE,
            label_selector=f"name={label_value}",
            field_selector="status.phase=Running",
        )
    except Exception as e:
        print(f"  carts_rotation._is_pod_ready({label_value!r}): real failure, "
              f"treating as not-ready (fail-closed): {e}")
        return False
    if not pods.items:
        return False
    pod = pods.items[0]
    for status in pod.status.container_statuses or []:
        if status.name == "carts" and status.ready:
            return True
    return False


def is_carts_ready() -> bool:
    return _is_pod_ready(CARTS_LABEL)


def is_carts_warm_ready() -> bool:
    return _is_pod_ready(CARTS_WARM_LABEL)


def _patch_selector(label_value: str) -> bool:
    """Fails closed -- returns False (never raises) on any real
    failure. Callers already only reach this point after confirming
    the target is Ready; a failure here means the write itself didn't
    land (RBAC, transient API issue, expired token), which the caller
    must treat as "flip did not happen," not crash over."""
    try:
        core = _core_v1()
        core.patch_namespaced_service(
            SERVICE_NAME, NAMESPACE,
            {"spec": {"selector": {"name": label_value}}},
        )
        return True
    except Exception as e:
        print(f"  carts_rotation._patch_selector({label_value!r}): real failure, "
              f"flip did not happen: {e}")
        return False


def flip_to_warm() -> bool:
    """The forward flip -- called immediately after actions.py's
    restart_deployment("carts") returns, from the orchestration/scorer
    layer, never from inside a scored action function. Fail-closed at
    every step: if carts-warm isn't confirmed Ready, or the underlying
    reads/writes hit any real failure, does nothing and returns False
    -- worst case is today's honest slow recovery, never a flip onto a
    not-actually-ready pod, and never an unhandled exception reaching
    the caller (the same guarantee locked in the original review, now
    also covering infra/auth failures, not just "not ready yet")."""
    if get_active_label() == CARTS_WARM_LABEL:
        return True  # already flipped, idempotent no-op
    if not is_carts_warm_ready():
        return False
    return _patch_selector(CARTS_WARM_LABEL)


def flip_to_carts() -> bool:
    """The backward flip -- restoration to steady state. Called by the
    detached background reconciliation process (restore_carts_active.py,
    spawned from p3_scorer.py) once carts is confirmed genuinely Ready
    again. NEVER issues a restart_deployment call -- only ever patches
    the selector. Same fail-closed guarantee as flip_to_warm()."""
    if get_active_label() == CARTS_LABEL:
        return True  # already restored, idempotent no-op
    if not is_carts_ready():
        return False
    return _patch_selector(CARTS_LABEL)
