"""
Standalone fault-injection baseline audit -- checks every fault class's
target config against its real documented baseline (from injector.py's own
constants, not re-derived here), independent of whether that class is
about to be tested. Every existing `_ensure_*_baseline` check in
injector.py only protects a class right before ITS OWN next injection --
nothing previously caught a leftover drift for a class that just isn't
being tested for a while.

Built 2026-07-28 after a real, expensive lesson: a `bad-rollout` episode
(or a manual debugging reproduction) left front-end's image patched to
its fault tag for 17 hours, undetected, and cost a long investigation
before being traced back to it. This script exists so that class of
problem is a 10-second check from now on, not a multi-hour investigation.

Usage:
    python3 check_all_baselines.py            # report-only, default
    python3 check_all_baselines.py --fix      # also reverts any drift found

Read-only unless --fix is passed. Uses the developer's own kubeconfig via
plain kubectl (same as injector.py's own _ensure_*_baseline checks) --
NOT the restricted wardence-agent SA, since this is an operator-run audit
tool, not something the agent itself calls.
"""

import argparse
import os
import subprocess
import sys

NAMESPACE = "sock-shop"

# Real baseline values, matching injector.py's own constants exactly --
# not re-derived or guessed. If injector.py's constants ever change,
# update here too (deliberately not importing injector.py directly, to
# keep this script standalone and safe to run even if injector.py itself
# is mid-edit or broken).
OOM_BASELINE_MEMORY_LIMIT = "200Mi"
CPU_THROTTLE_BASELINE_CPU_LIMIT = "300m"
UNDER_PROVISIONED_BASELINE_REPLICAS = 1
# Kept deliberately standalone (see this file's docstring) -- so it must be
# updated in lockstep with injector.py's own copy. Moved off upstream
# weaveworksdemos/front-end:0.3.12, 2026-08-26, to the patched multi-arch
# rebuild; see injector.py's copy for the full reasoning. NOTE: this file's
# drift-check REVERTS front-end to whatever this value is -- on 2026-08-24
# a stale value here silently re-broke a just-applied image fix mid-session.
FRONT_END_IMAGE_BASELINE = os.environ.get(
    "FRONT_END_IMAGE_BASELINE", "ghcr.io/aayush8392/front-end:0.3.12-wardence1"
)
PAYMENT_READINESS_PATH_BASELINE = "/health"
SESSION_DB_BASELINE_REPLICAS = 1
QUEUE_MASTER_EPHEMERAL_LIMIT_BASELINE = "300Mi"


def kubectl(args: list[str]) -> str:
    result = subprocess.run(["kubectl"] + args, capture_output=True, text=True)
    return result.stdout.strip()


def check_jsonpath(deployment: str, jsonpath: str, expected: str, label: str, drift: list):
    actual = kubectl([
        "get", "deployment", deployment, "-n", NAMESPACE,
        "-o", f"jsonpath={jsonpath}",
    ])
    if actual == expected:
        print(f"  OK    {label}: {actual}")
    else:
        print(f"  DRIFT {label}: real={actual!r} expected={expected!r}")
        drift.append((deployment, jsonpath, expected, label))


def check_dead_pods(drift: list):
    """Any Failed or Unknown-status pod in sock-shop is leftover cruft --
    same recurring queue-master issue already documented in
    wardence_buildlog.md's Standing Notes, generalized to check every
    service, not just queue-master."""
    output = kubectl([
        "get", "pods", "-n", NAMESPACE,
        "--field-selector=status.phase!=Running,status.phase!=Succeeded",
        "-o", "jsonpath={range .items[*]}{.metadata.name}{\" \"}{.status.phase}{\"\\n\"}{end}",
    ])
    dead = [line for line in output.splitlines() if line.strip()]
    if not dead:
        print("  OK    no dead/stuck pods")
    else:
        for line in dead:
            name = line.split()[0]
            print(f"  DRIFT dead pod: {line}")
            drift.append(("__pod__", name, None, "dead pod"))


def apply_fix(deployment: str, jsonpath: str, expected: str, label: str):
    if deployment == "__pod__":
        print(f"  FIXING deleting dead pod {jsonpath}...")
        kubectl(["delete", "pod", jsonpath, "-n", NAMESPACE])
        return

    print(f"  FIXING {label} -> {expected}...")
    if "resources.limits.memory" in jsonpath and deployment == "catalogue":
        patch = '{"spec":{"template":{"spec":{"containers":[{"name":"catalogue","resources":{"limits":{"memory":"' + expected + '"}}}]}}}}'
        kubectl(["patch", "deployment", deployment, "-n", NAMESPACE, "--type=strategic", "-p", patch])
    elif "resources.limits.cpu" in jsonpath and deployment == "user":
        patch = '{"spec":{"template":{"spec":{"containers":[{"name":"user","resources":{"limits":{"cpu":"' + expected + '"}}}]}}}}'
        kubectl(["patch", "deployment", deployment, "-n", NAMESPACE, "--type=strategic", "-p", patch])
    elif "image" in jsonpath:
        kubectl(["set", "image", f"deployment/{deployment}", f"{deployment}={expected}", "-n", NAMESPACE])
    elif "readinessProbe" in jsonpath:
        patch = '{"spec":{"template":{"spec":{"containers":[{"name":"payment","readinessProbe":{"httpGet":{"path":"' + expected + '"}}}]}}}}'
        kubectl(["patch", "deployment", deployment, "-n", NAMESPACE, "--type=strategic", "-p", patch])
    elif jsonpath == "__replicas__":
        kubectl(["scale", "deployment", deployment, "-n", NAMESPACE, f"--replicas={expected}"])
    else:
        print(f"  WARNING: no fix logic for {label}, skipping -- fix manually.")


def check_replicas(deployment: str, expected: int, label: str, drift: list):
    actual = kubectl([
        "get", "deployment", deployment, "-n", NAMESPACE,
        "-o", "jsonpath={.spec.replicas}",
    ])
    if actual == str(expected):
        print(f"  OK    {label}: {actual}")
    else:
        print(f"  DRIFT {label}: real={actual!r} expected={expected!r}")
        drift.append((deployment, "__replicas__", str(expected), label))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="revert any drift found")
    args = parser.parse_args()

    drift: list = []

    print("catalogue (oom baseline: memory limit)")
    check_jsonpath(
        "catalogue",
        "{.spec.template.spec.containers[0].resources.limits.memory}",
        OOM_BASELINE_MEMORY_LIMIT, "catalogue memory limit", drift,
    )

    print("\ncatalogue (under-provisioned-replicas baseline: replica count)")
    check_replicas("catalogue", UNDER_PROVISIONED_BASELINE_REPLICAS, "catalogue replicas", drift)

    print("\nuser (cpu-throttling baseline: cpu limit)")
    check_jsonpath(
        "user",
        "{.spec.template.spec.containers[0].resources.limits.cpu}",
        CPU_THROTTLE_BASELINE_CPU_LIMIT, "user cpu limit", drift,
    )

    print("\nfront-end (bad-rollout baseline: image tag)")
    check_jsonpath(
        "front-end",
        "{.spec.template.spec.containers[0].image}",
        FRONT_END_IMAGE_BASELINE, "front-end image", drift,
    )

    print("\npayment (init-failure baseline: readinessProbe path)")
    check_jsonpath(
        "payment",
        "{.spec.template.spec.containers[0].readinessProbe.httpGet.path}",
        PAYMENT_READINESS_PATH_BASELINE, "payment readinessProbe path", drift,
    )

    print("\nsession-db (session-cart-failure baseline: replica count)")
    check_replicas("session-db", SESSION_DB_BASELINE_REPLICAS, "session-db replicas", drift)

    print("\nqueue-master (disk-full baseline: ephemeral-storage limit)")
    check_jsonpath(
        "queue-master",
        "{.spec.template.spec.containers[0].resources.limits.ephemeral-storage}",
        QUEUE_MASTER_EPHEMERAL_LIMIT_BASELINE, "queue-master ephemeral-storage limit", drift,
    )

    print("\ndead/stuck pods (any service)")
    check_dead_pods(drift)

    print()
    if not drift:
        print("=== All baselines clean, no dead pods. ===")
        return

    print(f"=== {len(drift)} drift item(s) found. ===")
    if args.fix:
        print("\nApplying fixes...")
        for deployment, jsonpath, expected, label in drift:
            apply_fix(deployment, jsonpath, expected, label)
        print("\nDone. Rerun without --fix to confirm everything is clean now.")
    else:
        print("Rerun with --fix to revert these automatically.")
        sys.exit(1)


if __name__ == "__main__":
    main()
