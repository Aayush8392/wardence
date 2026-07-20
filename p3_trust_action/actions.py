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
    kubectl create token wardence-agent -n sock-shop --duration=24h > p3_trust_action/sa_token.txt
Regenerate when it expires (401 errors are the signal).
"""

import os

from kubernetes import client, config

SA_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "sa_token.txt")
DEFAULT_NAMESPACE = "sock-shop"


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
    cfg.api_key = {"authorization": f"Bearer {token}"}
    cfg.api_key_prefix = {}

    return client.AppsV1Api(client.ApiClient(cfg))


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
    """Fix for disk-full (ephemeral): scale to 0 then back up to clear ephemeral storage."""
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


# Fixed, named allowlist -- this dict IS the cage at the code level.
# The trust engine and agent must only ever call through here, never
# import the functions above directly, and never construct free-form
# k8s API calls.
ALLOWED_ACTIONS = {
    "restart_deployment": restart_deployment,
    "patch_memory_limit": patch_memory_limit,
    "scale_deployment": scale_deployment,
}
