"""
P3 typed action layer: the ONLY way the agent is allowed to touch the
cluster. Every action:
  1. Authenticates as the restricted `wardence-agent` ServiceAccount
     (see manifests/rbac.yaml) -- NOT the developer's kubeconfig identity.
     A disallowed call fails with a real 403 from the k8s API server.
  2. Runs a server-side dry-run first. Only proceeds to the real call
     if the dry-run succeeds.
  3. Is one of a fixed, named function -- no free-form kubectl/API calls.

Requires a live SA token at p3_trust_action/sa_token.txt:
    kubectl create token wardence-agent -n sock-shop --duration=720h > p3_trust_action/sa_token.txt

30 days (720h), not permanent -- deliberately. Kubernetes deprecated
permanent auto-mounted SA tokens (pre-1.24) specifically because a
leaked/forgotten permanent credential is a much bigger liability than
a bounded one; kubectl create token (TokenRequest API) is bounded-
lifetime by design, no "forever" option without working against that.
Also thematically consistent with this whole project: trust here is
supposed to be earned/bounded, not permanently granted, and that
should hold for the action layer's own credential too. 24h (the
original duration) was arbitrary and too easy to silently run past
mid-session -- confirmed the hard way (2026-07-21): a stale, EXPIRED
token caused every real oom fix attempt to fail with 401, which the
trust engine correctly (if confusingly, until diagnosed) read as "fix
didn't apply" and used to demote oom out of can_act. 720h gives a
comfortable, session-spanning margin while still keeping the
rotation principle intact.

_apps_v1() checks the token's own `exp` claim before use and raises a
clear, loud error immediately if it's expired or expiring soon,
instead of letting a stale token surface later as a confusing 401 deep
inside a fix attempt (which is exactly what happened before this
check existed).
"""

import base64
import datetime
import json
import os
import time

from kubernetes import client, config

SA_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "sa_token.txt")
DEFAULT_NAMESPACE = "sock-shop"

# Live step-progress for multi-step fixes (2026-07-22) -- so a frontend
# can poll "scaling down -> waiting for termination -> scaling up"
# instead of staring at a content-free spinner for the ~1-2 minutes
# restore_from_disk_full genuinely takes. In-memory only, not persisted:
# this is transient run state, not the final verdict (that's already
# captured properly in episode_snapshots via p3_scorer.py). Keyed by
# (namespace, name) since only one fix runs at a time system-wide
# (matches the Operator API's existing one-episode-in-flight rule) --
# no locking needed for the same reason, and a new run overwrites the
# previous entry rather than appending forever.
_PROGRESS: dict[tuple[str, str], list[dict]] = {}


def _reset_progress(name: str, namespace: str):
    _PROGRESS[(namespace, name)] = []


def _record_step(name: str, namespace: str, step: str, status: str, detail: str | None = None):
    _PROGRESS.setdefault((namespace, name), []).append(
        {
            "step": step,
            "status": status,
            "detail": detail,
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )


def get_progress(name: str, namespace: str) -> list[dict]:
    """Read-only accessor for the frontend's live-trigger view to poll."""
    return _PROGRESS.get((namespace, name), [])

# Warn/refuse this far ahead of actual expiry, not just at the exact
# moment it lapses -- gives enough lead time to regenerate before a
# real fix attempt gets caught mid-episode.
TOKEN_EXPIRY_WARNING_HOURS = 24

# Must comfortably EXCEED the target's terminationGracePeriodSeconds --
# queue-master's is 30s (confirmed directly, not assumed), so a 30s wait
# was a guaranteed race: Kubernetes may use the full grace period before
# SIGKILLing, so the wait expired at the exact moment the pod was still
# legitimately terminating (found 2026-07-22 -- caused a real
# applied=False plus a deployment left stuck at replicas=0).
POD_TERMINATE_TIMEOUT_S = 60
POD_START_TIMEOUT_S = 60


def _check_token_expiry(token: str):
    """Decodes the JWT's own `exp` claim directly (no signature
    verification needed, we're only reading a field we already trust
    since it came from our own sa_token.txt) and raises immediately if
    the token is expired or within TOKEN_EXPIRY_WARNING_HOURS of
    expiring -- found the hard way that a silently-expired token
    doesn't fail loudly, it just makes every real API call 401 and
    looks like an application-level failure."""
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    exp = payload.get("exp")
    if exp is None:
        return  # no exp claim -- can't check, don't block on it
    seconds_left = exp - time.time()
    if seconds_left < 0:
        raise RuntimeError(
            f"sa_token.txt EXPIRED {abs(seconds_left) / 3600:.1f}h ago. Regenerate with: "
            f"kubectl create token wardence-agent -n sock-shop --duration=720h > "
            f"{SA_TOKEN_PATH}"
        )
    if seconds_left < TOKEN_EXPIRY_WARNING_HOURS * 3600:
        print(
            f"WARNING: sa_token.txt expires in {seconds_left / 3600:.1f}h -- regenerate soon: "
            f"kubectl create token wardence-agent -n sock-shop --duration=720h > "
            f"{SA_TOKEN_PATH}"
        )


def _apps_v1() -> client.AppsV1Api:
    """AppsV1Api client authenticated as the wardence-agent ServiceAccount."""
    config.load_kube_config()  # host/CA info only
    cfg = client.Configuration.get_default_copy()

    # load_kube_config() pulls in YOUR client-cert identity from the
    # kubeconfig (k3s defaults to mTLS, not tokens). If left in place,
    # the API server authenticates via that cert and silently ignores
    # the SA token below -- every call would run as full admin, not
    # as the restricted wardence-agent SA. Must be stripped.
    cfg.cert_file = None
    cfg.key_file = None

    with open(SA_TOKEN_PATH) as f:
        token = f.read().strip()
    _check_token_expiry(token)
    cfg.api_key = {"authorization": f"Bearer {token}"}
    cfg.api_key_prefix = {}

    return client.AppsV1Api(client.ApiClient(cfg))


def _core_v1() -> client.CoreV1Api:
    """CoreV1Api twin of _apps_v1() -- same auth setup, needed to list
    pods directly (AppsV1Api only covers Deployments/ReplicaSets)."""
    config.load_kube_config()
    cfg = client.Configuration.get_default_copy()
    cfg.cert_file = None
    cfg.key_file = None
    with open(SA_TOKEN_PATH) as f:
        token = f.read().strip()
    _check_token_expiry(token)
    cfg.api_key = {"authorization": f"Bearer {token}"}
    cfg.api_key_prefix = {}
    return client.CoreV1Api(client.ApiClient(cfg))


def _wait_for_pod_count(name: str, namespace: str, expected_count: int, timeout_s: int = 30) -> bool:
    """Polls until exactly expected_count pods match label name=<name>.
    Needed because patch_namespaced_deployment_scale returns as soon as
    the API server ACCEPTS the patch, not once it's actually reconciled
    -- found 2026-07-22: restore_from_disk_full's scale-to-0-then-back-
    to-1 calls, issued back-to-back with no wait, could report
    applied=True on both while Kubernetes coalesced them into a no-op
    (the old, already-contaminated pod never actually got deleted and
    replaced), silently defeating the entire point of the fix. Returns
    False on timeout rather than raising -- caller decides how to treat
    a fix that couldn't be confirmed."""
    api = _core_v1()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pods = api.list_namespaced_pod(namespace, label_selector=f"name={name}")
        # Count only LIVE pods (phase == "Running"). This deliberately
        # excludes Failed/Unknown pods: evicted disk-full pods pile up
        # as Failed objects that kubelet does NOT garbage-collect on
        # this cluster (queue-master routinely has a dozen+ dead pods
        # sitting around), so counting every object would never reach 0
        # and this wait would always time out -- which is exactly what
        # left the deployment stuck at replicas=0 (found 2026-07-22, the
        # first version counted len(pods.items) and broke disk-full's
        # own fix). A gracefully terminating pod keeps phase=="Running"
        # until it is actually removed, so it is still counted here --
        # preserving the real guarantee restore_from_disk_full needs:
        # wait until the old, contaminated pod is genuinely gone before
        # scaling back up, not merely until the API accepted the patch.
        running = [p for p in pods.items if p.status.phase == "Running"]
        if len(running) == expected_count:
            return True
        time.sleep(2)
    return False


def _patch_deployment(name: str, namespace: str, body: dict) -> dict:
    """Dry-run a deployment patch, then apply for real if the dry-run succeeds."""
    api = _apps_v1()

    try:
        api.patch_namespaced_deployment(
            name=name, namespace=namespace, body=body, dry_run="All"
        )
    except client.ApiException as e:
        return {"dry_run_ok": False, "applied": False, "error": f"{e.status}: {e.reason}"}

    try:
        api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except client.ApiException as e:
        return {"dry_run_ok": True, "applied": False, "error": f"{e.status}: {e.reason}"}

    return {"dry_run_ok": True, "applied": True, "error": None}


def restart_deployment(name: str, namespace: str = DEFAULT_NAMESPACE) -> dict:
    """Fix for crash-loop: rollout restart via a pod-template annotation bump."""
    import datetime

    restarted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}
                }
            }
        }
    }
    result = _patch_deployment(name, namespace, body)
    return {"action": "restart_deployment", "target": name, "namespace": namespace, **result}


def patch_memory_limit(
    name: str, container: str, limit: str, namespace: str = DEFAULT_NAMESPACE
) -> dict:
    """Fix for OOM: raise a container's memory limit."""
    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": container, "resources": {"limits": {"memory": limit}}}
                    ]
                }
            }
        }
    }
    result = _patch_deployment(name, namespace, body)
    return {
        "action": "patch_memory_limit",
        "target": name,
        "container": container,
        "limit": limit,
        "namespace": namespace,
        **result,
    }


def patch_cpu_limit(
    name: str, container: str, limit: str, namespace: str = DEFAULT_NAMESPACE
) -> dict:
    """Fix for cpu-throttling: raise a container's CPU limit.

    Reworked 2026-07-28 to resize the LIVE pod in place via the
    pods/resize subresource (KEP-1287) instead of patching the
    Deployment template -- a Deployment-template patch forces a full
    pod replace-and-wait every time, real cost confirmed via this
    project's own timing data (~280s of a single cpu-throttling
    episode's ~380s total was this rollout wait alone). Manually
    verified first (not assumed) that this cluster (k3s v1.36) actually
    supports in-place resize without a restart: cpu limit changed on a
    live pod, restart count stayed at 0 throughout. Requires the
    complete current resources block in the patch, not just the
    changed field -- the resize API rejects a partial patch as "removing"
    whatever fields it omits, confirmed via a real 422 on the first
    attempt.

    Needs a real RBAC cage expansion to work: `patch` on the `pods/resize`
    subresource specifically (added to manifests/rbac.yaml 2026-07-28) --
    deliberately scoped to resize only, NOT a general pods-patch grant,
    consistent with the cage's existing minimal-privilege principle.

    Falls back to the original Deployment-patch-and-wait path (still
    correct, just slower) if the live pod can't be found or the resize
    attempt fails for any reason -- this fix must never silently do
    nothing."""
    api = _core_v1()
    pods = api.list_namespaced_pod(namespace, label_selector=f"name={name}")
    running = [p for p in pods.items if p.status.phase == "Running"]

    if running:
        pod = running[0]
        pod_name = pod.metadata.name
        try:
            current_container = next(
                c for c in pod.spec.containers if c.name == container
            )
            resources = current_container.to_dict().get("resources") or {}
            resources.setdefault("limits", {})["cpu"] = limit
            resize_body = {
                "spec": {"containers": [{"name": container, "resources": resources}]}
            }
            try:
                api.patch_namespaced_pod_resize(
                    name=pod_name, namespace=namespace, body=resize_body, dry_run="All"
                )
                api.patch_namespaced_pod_resize(
                    name=pod_name, namespace=namespace, body=resize_body
                )
                return {
                    "action": "patch_cpu_limit",
                    "target": name,
                    "container": container,
                    "limit": limit,
                    "namespace": namespace,
                    "dry_run_ok": True,
                    "applied": True,
                    "error": None,
                    "method": "in_place_resize",
                    "pod": pod_name,
                }
            except client.ApiException as e:
                # Real resize attempt failed -- fall through to the
                # Deployment-patch path below rather than reporting
                # applied=False (this fix must still be attempted, just
                # via the slower mechanism), but keep the real error
                # visible for whoever's reading logs.
                print(f"  in-place resize failed for {pod_name} ({e.status}: {e.reason}), "
                      f"falling back to a Deployment patch...")
        except StopIteration:
            print(f"  container {container} not found on {pod_name}, "
                  f"falling back to a Deployment patch...")

    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": container, "resources": {"limits": {"cpu": limit}}}
                    ]
                }
            }
        }
    }
    result = _patch_deployment(name, namespace, body)
    return {
        "action": "patch_cpu_limit",
        "target": name,
        "container": container,
        "limit": limit,
        "namespace": namespace,
        "method": "deployment_patch_fallback",
        **result,
    }


def rollback_deployment(name: str, namespace: str = DEFAULT_NAMESPACE) -> dict:
    """Fix for bad-rollout: reverts a Deployment to its most recent
    PREVIOUS revision's pod template -- the same real operation
    `kubectl rollout undo` performs, reimplemented via the Python
    client directly rather than shelling out to kubectl, which would
    run as the developer's own admin kubeconfig identity instead of
    the restricted wardence-agent SA, breaking the blast-radius cage.
    RBAC confirmed sufficient empirically before this was built
    (2026-07-24, check_rollout_undo_rbac.sh): patch on deployments,
    get/list on replicasets -- no cage expansion needed. The removed
    pre-1.16 deployments/rollback subresource is NOT used here."""
    apps_api = _apps_v1()

    try:
        deployment = apps_api.read_namespaced_deployment(name, namespace)
    except client.ApiException as e:
        return {
            "action": "rollback_deployment", "target": name, "namespace": namespace,
            "dry_run_ok": False, "applied": False, "error": f"{e.status}: {e.reason}",
        }

    current_revision = int(
        (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
    )

    try:
        rs_list = apps_api.list_namespaced_replica_set(namespace, label_selector=f"name={name}")
    except client.ApiException as e:
        return {
            "action": "rollback_deployment", "target": name, "namespace": namespace,
            "dry_run_ok": False, "applied": False, "error": f"{e.status}: {e.reason}",
        }

    # Same algorithm `kubectl rollout undo` uses with no explicit
    # --to-revision: the most recent revision STRICTLY LESS than the
    # current one, not necessarily current-1 (revisions can be
    # skipped/pruned).
    previous_candidates = [
        (int(rs.metadata.annotations["deployment.kubernetes.io/revision"]), rs)
        for rs in rs_list.items
        if rs.metadata.annotations and "deployment.kubernetes.io/revision" in rs.metadata.annotations
        and int(rs.metadata.annotations["deployment.kubernetes.io/revision"]) < current_revision
    ]
    if not previous_candidates:
        return {
            "action": "rollback_deployment", "target": name, "namespace": namespace,
            "dry_run_ok": False, "applied": False,
            "error": "no previous revision found to roll back to",
        }
    previous_candidates.sort(key=lambda pair: pair[0])
    _, target_rs = previous_candidates[-1]

    template_dict = client.ApiClient().sanitize_for_serialization(target_rs.spec.template)
    body = {"spec": {"template": template_dict}}
    result = _patch_deployment(name, namespace, body)
    return {"action": "rollback_deployment", "target": name, "namespace": namespace, **result}


def scale_deployment(name: str, replicas: int, namespace: str = DEFAULT_NAMESPACE) -> dict:
    """Low-level primitive: one scale call to a given replica count. NOT
    a disk-full fix by itself -- see restore_from_disk_full below, which
    composes two calls (0, then back up) since that's what actually
    clears a pod's ephemeral-storage breach."""
    api = _apps_v1()
    body = {"spec": {"replicas": replicas}}

    try:
        api.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body=body, dry_run="All"
        )
    except client.ApiException as e:
        return {
            "action": "scale_deployment",
            "target": name,
            "replicas": replicas,
            "namespace": namespace,
            "dry_run_ok": False,
            "applied": False,
            "error": f"{e.status}: {e.reason}",
        }

    try:
        api.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=body)
    except client.ApiException as e:
        return {
            "action": "scale_deployment",
            "target": name,
            "replicas": replicas,
            "namespace": namespace,
            "dry_run_ok": True,
            "applied": False,
            "error": f"{e.status}: {e.reason}",
        }

    return {
        "action": "scale_deployment",
        "target": name,
        "replicas": replicas,
        "namespace": namespace,
        "dry_run_ok": True,
        "applied": True,
        "error": None,
    }


def restore_from_disk_full(name: str, namespace: str = DEFAULT_NAMESPACE, replicas: int = 1) -> dict:
    """
    Fix for disk-full: a single scale call clears nothing -- the pod's
    writable layer (where the ephemeral-storage breach actually lives)
    only gets wiped when the pod itself is replaced. Scaling to 0 then
    back up forces exactly that: the old pod (and its full disk) is
    deleted, the new one starts clean. Composes scale_deployment rather
    than duplicating its dry-run/apply logic.

    Waits for the pod count to actually reach 0 before scaling back up
    -- found 2026-07-22: without this wait, scale-to-0 and scale-to-1
    issued back-to-back could get coalesced by Kubernetes into a no-op
    (patch_namespaced_deployment_scale returns success the instant the
    API server ACCEPTS a patch, not once it's reconciled), leaving the
    SAME, already-contaminated pod running the whole time -- action_
    applied would report True while the fault silently never actually
    cleared, then reappeared minutes later and got misread as a fresh
    post-fix flap.
    """
    _reset_progress(name, namespace)

    _record_step(name, namespace, "scaling_down", "in_progress")
    scale_down = scale_deployment(name, 0, namespace)
    if not scale_down["applied"]:
        _record_step(name, namespace, "scaling_down", "failed", scale_down.get("error"))
        return {
            "action": "restore_from_disk_full",
            "target": name,
            "namespace": namespace,
            "applied": False,
            "error": f"scale-to-0 failed: {scale_down.get('error')}",
            "scale_down": scale_down,
            "scale_up": None,
            # Real, timestamped step log -- previously only ever readable
            # live via GET /progress (in-memory, wiped on the next episode),
            # never persisted. Capturing it into the return value means it
            # flows through p3_agent.py -> p3_scorer.py -> episode_snapshots.
            # action_result -> episodes.json with no schema change needed,
            # so a HISTORICAL replay can show the same 4-step sequence
            # Operator's live view already shows, not just 2 sub-results.
            "progress_log": get_progress(name, namespace),
        }
    _record_step(name, namespace, "scaling_down", "done")

    _record_step(name, namespace, "waiting_for_termination", "in_progress")
    pod_gone = _wait_for_pod_count(
        name, namespace, expected_count=0, timeout_s=POD_TERMINATE_TIMEOUT_S
    )
    _record_step(
        name, namespace, "waiting_for_termination", "done" if pod_gone else "timed_out"
    )

    # ALWAYS scale back up, even if the terminate-wait timed out. Leaving
    # the deployment at replicas=0 is far worse than a failed fix: it
    # takes the service down for every SUBSEQUENT episode too, and the
    # injector then finds no Running pod and reports a misleading "no
    # eviction detected" (found 2026-07-22 -- this exact state wedged the
    # cluster twice). A timed-out wait still reports applied=False below,
    # so the fix is judged honestly; it just doesn't leave wreckage.
    _record_step(name, namespace, "scaling_up", "in_progress")
    scale_up = scale_deployment(name, replicas, namespace)
    if scale_up["applied"]:
        _record_step(name, namespace, "scaling_up", "done")
        _record_step(name, namespace, "waiting_for_new_pod", "in_progress")
        pod_back = _wait_for_pod_count(
            name, namespace, expected_count=replicas, timeout_s=POD_START_TIMEOUT_S
        )
        if not pod_back:
            scale_up = {**scale_up, "applied": False, "error": "timed out waiting for new pod to start Running"}
            _record_step(name, namespace, "waiting_for_new_pod", "timed_out")
        else:
            _record_step(name, namespace, "waiting_for_new_pod", "done")
    else:
        _record_step(name, namespace, "scaling_up", "failed", scale_up.get("error"))

    if not pod_gone:
        _record_step(name, namespace, "complete", "failed", "old pod never terminated")
        return {
            "action": "restore_from_disk_full",
            "target": name,
            "namespace": namespace,
            "applied": False,
            "error": (
                "timed out waiting for old pod to terminate before scaling back up -- "
                "the fix cannot be trusted to have cleared the disk (scaled back up "
                "anyway to avoid leaving the deployment at 0)"
            ),
            "scale_down": scale_down,
            "scale_up": scale_up,
            "progress_log": get_progress(name, namespace),
        }

    _record_step(name, namespace, "complete", "done" if scale_up["applied"] else "failed")
    return {
        "action": "restore_from_disk_full",
        "target": name,
        "namespace": namespace,
        "applied": scale_up["applied"],
        "error": scale_up.get("error"),
        "scale_down": scale_down,
        "scale_up": scale_up,
        "progress_log": get_progress(name, namespace),
    }


# Fixed, named allowlist -- this dict IS the cage at the code level.
# The trust engine and agent must only ever call through here, never
# import the functions above directly, and never construct free-form
# k8s API calls.
ALLOWED_ACTIONS = {
    "restart_deployment": restart_deployment,
    "patch_memory_limit": patch_memory_limit,
    "patch_cpu_limit": patch_cpu_limit,
    "scale_deployment": scale_deployment,
    "rollback_deployment": rollback_deployment,
    "restore_from_disk_full": restore_from_disk_full,
}
