"""
Phase H step 2: semantic tool-call validator.

Sits IN FRONT of the existing RBAC cage (actions.py's ALLOWED_ACTIONS +
its own server-side dry-run) -- does NOT replace it. Locked policy
(wardence_context.md's Model Strategy, item 6): reject a malformed or
out-of-policy tool call before it ever reaches the cage. This exists
specifically for LLM-generated tool calls (the ReAct loop, step 3) --
scripted/deterministic callers already go through actions.py directly
and don't need this extra layer.

A rejection here must force report_only for that episode and log the
rejection reason -- this file only decides ok/not-ok + why; the caller
(step 3's ReAct loop) is responsible for actually enforcing the
report_only fallback and logging it into the episode record.

Checks performed, in order:
  1. tool name is in the real ALLOWED_ACTIONS allowlist (actions.py).
  2. required parameters for that tool are present and correctly typed.
  3. namespace, if supplied, is exactly "sock-shop" -- the only real
     namespace the cage ever operates in. (The wrapper functions default
     to this already; a validator's job is to catch an LLM that tries to
     supply anything else at all, not to rely on the default silently
     saving it.)
  4. for patch-shaped actions (patch_memory_limit, patch_cpu_limit): the
     limit value must be a syntactically valid Kubernetes resource
     quantity (e.g. "500Mi", "300m", "1Gi") -- rejects anything that
     isn't a real patchable resource-limit value. Real actions.py
     functions take typed args (name/container/limit), not a raw patch
     body, so this is the equivalent enforcement adapted to this
     project's real cage shape.
  5. for scale-shaped actions (scale_deployment, restore_from_disk_full):
     replicas must be a small non-negative int within a sane bound
     (0-20) -- guards against a malformed/hallucinated absurd value
     ever reaching a real dry-run.
  6. no unexpected extra params -- an LLM tool call carrying a field
     no real action function accepts is rejected rather than silently
     ignored.

NOT in scope here: the RBAC cage itself (unchanged, still the real
enforcement layer), the ReAct loop that calls this before dispatching to
actions.ALLOWED_ACTIONS (step 3), and deciding what the report_only
fallback actually looks like end-to-end (also step 3).
"""
import re
from typing import Optional

from actions import ALLOWED_ACTIONS, DEFAULT_NAMESPACE

# Real gap found via Kimi review 12 (2026-07-30), confirmed against this
# file directly before fixing: the original _is_valid_quantity checked
# SHAPE only (does it look like "500Mi"), never MAGNITUDE -- so
# "999999Gi" passed cleanly. The validator's whole job is to catch a
# syntactically-valid-but-operationally-absurd value before it ever
# reaches the real cage; a shape-only check doesn't do that. Split into
# two tool-specific quantity validators (memory vs. cpu use different
# suffix grammars and real sane ranges for THIS lab's node) with a real
# magnitude cap, generous enough for any real legitimate fix on this
# project's cluster (node allocatable ~9.7Gi memory) but rejecting an
# obviously-hallucinated value.
MAX_REPLICAS = 20
MAX_MEMORY_LIMIT_MI = 2048  # 2Gi -- generous vs. every real limit this project has ever used (200-400Mi)
MAX_CPU_LIMIT_M = 2000  # 2 cores -- generous vs. every real limit this project has ever used (300-600m)

_MEMORY_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti)?$")
_CPU_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)(m)?$")
_MI_FACTORS = {"": 1 / (1024 * 1024), "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}


def _is_valid_memory_quantity(value) -> bool:
    if not isinstance(value, str):
        return False
    match = _MEMORY_QUANTITY_RE.match(value)
    if not match:
        return False
    mi_value = float(match.group(1)) * _MI_FACTORS[match.group(2) or ""]
    return 0 < mi_value <= MAX_MEMORY_LIMIT_MI


def _is_valid_cpu_quantity(value) -> bool:
    if not isinstance(value, str):
        return False
    match = _CPU_QUANTITY_RE.match(value)
    if not match:
        return False
    millicores = float(match.group(1)) * (1 if match.group(2) == "m" else 1000)
    return 0 < millicores <= MAX_CPU_LIMIT_M


def _is_valid_replicas(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_REPLICAS


def _is_nonempty_str(value) -> bool:
    return isinstance(value, str) and len(value.strip()) > 0


# Per-tool required params + a validator for each. Every tool in
# ALLOWED_ACTIONS must appear here -- checked by a self-test at import
# time (below) so a newly-added action can never silently skip
# validation by omission.
TOOL_SCHEMAS = {
    "restart_deployment": {
        "name": _is_nonempty_str,
    },
    "patch_memory_limit": {
        "name": _is_nonempty_str,
        "container": _is_nonempty_str,
        "limit": _is_valid_memory_quantity,
    },
    "patch_cpu_limit": {
        "name": _is_nonempty_str,
        "container": _is_nonempty_str,
        "limit": _is_valid_cpu_quantity,
    },
    "scale_deployment": {
        "name": _is_nonempty_str,
        "replicas": _is_valid_replicas,
    },
    "rollback_deployment": {
        "name": _is_nonempty_str,
    },
    # Real bug found and fixed 2026-08-06, live: "replicas" required here
    # went stale the moment action_proposer.py's 2026-08-03 fix removed
    # replicas from restore_from_disk_full's decision space entirely
    # (the LLM is never asked for it, never shown it in the prompt
    # schema -- action_proposer.py injects the real, tested value
    # server-side, AFTER this validator runs). Requiring it here meant
    # every real proposal from every provider was rejected before ever
    # reaching that injection step -- confirmed live: 100% of disk-full's
    # action proposals across an entire 5-hour, 84-episode batch fell
    # through to the deterministic fallback for this exact reason, the
    # same identical rejection on every single attempt.
    "restore_from_disk_full": {
        "name": _is_nonempty_str,
    },
}

_missing = set(ALLOWED_ACTIONS) - set(TOOL_SCHEMAS)
if _missing:
    raise RuntimeError(
        f"tool_call_validator.py is missing a schema for real cage action(s) {_missing} -- "
        "every entry in actions.ALLOWED_ACTIONS must have a validator here before it can be "
        "safely exposed to an LLM-generated tool call."
    )


def validate_tool_call(tool_name: str, params: dict) -> tuple[bool, Optional[str]]:
    """
    Returns (True, None) if the call is safe to pass to the real cage,
    or (False, reason) if it should be rejected before ever reaching
    actions.py. Never raises -- always returns a verdict.
    """
    if tool_name not in ALLOWED_ACTIONS:
        return False, f"tool '{tool_name}' is not in the real action allowlist"

    if not isinstance(params, dict):
        return False, f"params must be a dict, got {type(params).__name__}"

    namespace = params.get("namespace")
    if namespace is not None and namespace != DEFAULT_NAMESPACE:
        return False, f"namespace must be '{DEFAULT_NAMESPACE}', got {namespace!r}"

    schema = TOOL_SCHEMAS[tool_name]
    for param_name, validator in schema.items():
        if param_name not in params:
            return False, f"'{tool_name}' is missing required param '{param_name}'"
        if not validator(params[param_name]):
            return False, f"'{tool_name}' param '{param_name}' failed validation: {params[param_name]!r}"

    unexpected = set(params) - set(schema) - {"namespace"}
    if unexpected:
        return False, f"'{tool_name}' received unexpected param(s): {unexpected}"

    return True, None
