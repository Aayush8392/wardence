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
import json
import os
import time

from kubernetes import client, config

SA_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "sa_token.txt")
DEFAULT_NAMESPACE = "sock-shop"

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
    scale_down = scale_deployment(name, 0, namespace)
    if not scale_down["applied"]:
        return {
            "action": "restore_from_disk_full",
            "target": name,
            "namespace": namespace,
            "applied": False,
            "error": f"scale-to-0 failed: {scale_down.get('error')}",
            "scale_down": scale_down,
            "scale_up": None,
        }

    pod_gone = _wait_for_pod_count(
        name, namespace, expected_count=0, timeout_s=POD_TERMINATE_TIMEOUT_S
    )

    # ALWAYS scale back up, even if the terminate-wait timed out. Leaving
    # the deployment at replicas=0 is far worse than a failed fix: it
    # takes the service down for every SUBSEQUENT episode too, and the
    # injector then finds no Running pod and reports a misleading "no
    # eviction detected" (found 2026-07-22 -- this exact state wedged the
    # cluster twice). A timed-out wait still reports applied=False below,
    # so the fix is judged honestly; it just doesn't leave wreckage.
    scale_up = scale_deployment(name, replicas, namespace)
    if scale_up["applied"]:
        pod_back = _wait_for_pod_count(
            name, namespace, expected_count=replicas, timeout_s=POD_START_TIMEOUT_S
        )
        if not pod_back:
            scale_up = {**scale_up, "applied": False, "error": "timed out waiting for new pod to start Running"}

    if not pod_gone:
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
        }

    return {
        "action": "restore_from_disk_full",
        "target": name,
        "namespace": namespace,
        "applied": scale_up["applied"],
        "error": scale_up.get("error"),
        "scale_down": scale_down,
        "scale_up": scale_up,
    }


# Fixed, named allowlist -- this dict IS the cage at the code level.
# The trust engine and agent must only ever call through here, never
# import the functions above directly, and never construct free-form
# k8s API calls.
ALLOWED_ACTIONS = {
    "restart_deployment": restart_deployment,
    "patch_memory_limit": patch_memory_limit,
    "scale_deployment": scale_deployment,
    "restore_from_disk_full": restore_from_disk_full,
}
