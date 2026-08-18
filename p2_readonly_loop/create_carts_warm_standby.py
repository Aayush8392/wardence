"""
Creates the real, permanent `carts-warm` standby Deployment for
crash-loop's demo-visibility fix (Model A, locked spec:
wardence_crash_loop_warm_standby_LOCKED_SPEC.md).

Real, reusable setup infra -- not a throwaway probe script, committed
to the repo (matches this project's create_admin_account.py /
create_viewer_account.py precedent for one-time-but-reusable bootstrap
scripts).

What it does:
  1. Reads the REAL current `carts` Deployment spec from the live
     cluster (image, resources, probes -- including the liveness-probe
     fix already applied live -- serviceAccount, everything) via
     `kubectl get -o json`. Never hand-typed/approximated.
  2. Clones it into a new Deployment named `carts-warm`, with:
     - Its own distinct pod-template label (`name: carts-warm`) --
       this is what keeps its pods out of the real `carts` Service's
       selector (which matches `name: carts`) AND what keeps every
       existing Prometheus regex (`carts-[^-]+-[^-]+$`) from ever
       matching it, since `carts-warm-<hash>-<random>` has one extra
       hyphen segment. Confirmed via the locked spec -- this naming
       trick is real and load-bearing, not cosmetic.
     - replicas: 1 (permanent standby, not scaled to 0 between faults
       -- it needs to be genuinely warm at all times).
  3. Applies it and polls for the new pod to report Ready, using the
     same real check every other probe script in this project uses
     (containerStatuses[?(@.name=="carts")].ready), not just "Running".

Run once, manually, to set up the standby. Re-running it is safe
(idempotent `kubectl apply`) -- e.g. after `carts`' own spec changes
(a new image, a probe retune) and you want carts-warm to pick up the
same real config.

Usage: python3 create_carts_warm_standby.py
"""

import json
import subprocess
import sys
import time

NAMESPACE = "sock-shop"
SOURCE_NAME = "carts"
STANDBY_NAME = "carts-warm"
POLL_INTERVAL_S = 5
TIMEOUT_S = 900


def _kubectl_json(*args: str) -> dict:
    result = subprocess.run(
        ["kubectl", *args, "-o", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: kubectl {' '.join(args)} failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def build_standby_manifest(source: dict) -> dict:
    pod_spec = source["spec"]["template"]["spec"]  # real containers, resources, probes, SA -- unmodified
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": STANDBY_NAME,
            "namespace": NAMESPACE,
            "labels": {"name": STANDBY_NAME},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"name": STANDBY_NAME}},
            "template": {
                "metadata": {"labels": {"name": STANDBY_NAME}},
                "spec": pod_spec,
            },
        },
    }


def apply_manifest(manifest: dict) -> None:
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(manifest), capture_output=True, text=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(f"ERROR applying carts-warm manifest:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)


def wait_for_ready() -> bool:
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        result = subprocess.run(
            [
                "kubectl", "get", "pods", "-n", NAMESPACE,
                "-l", f"name={STANDBY_NAME}",
                "--field-selector=status.phase=Running",
                "-o", "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True, text=True,
        )
        pod_name = result.stdout.strip()
        if pod_name:
            ready_result = subprocess.run(
                [
                    "kubectl", "get", "pod", pod_name, "-n", NAMESPACE,
                    "-o", 'jsonpath={.status.containerStatuses[?(@.name=="carts")].ready}',
                ],
                capture_output=True, text=True,
            )
            if ready_result.stdout.strip() == "true":
                print(f"carts-warm pod ({pod_name}) is real, live, Ready.")
                return True
        time.sleep(POLL_INTERVAL_S)
    return False


def main() -> None:
    print(f"Reading real current '{SOURCE_NAME}' Deployment spec (live cluster, not assumed)...")
    source = _kubectl_json("get", "deployment", SOURCE_NAME, "-n", NAMESPACE)

    print(f"Cloning into a new permanent standby Deployment '{STANDBY_NAME}'...")
    manifest = build_standby_manifest(source)
    apply_manifest(manifest)

    print("Waiting for carts-warm to report real Ready (may take several minutes -- "
          "this is its own first-ever real JVM cold start)...")
    if wait_for_ready():
        print("Done. carts-warm is up, healthy, and excluded from the real Service "
              "(no traffic routed to it -- it's not in the carts Service's selector).")
    else:
        print(f"TIMED OUT after {TIMEOUT_S}s waiting for carts-warm to become Ready. "
              "Check `kubectl describe pod -n sock-shop -l name=carts-warm` for the real cause.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
