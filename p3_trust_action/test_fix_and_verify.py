"""One-off script: apply the crash-loop fix, then confirm it holds for
the full durability window before declaring victory.
Run: python3 p3_trust_action/test_fix_and_verify.py  (from repo root,
with wardence_venv active and `kubectl port-forward -n monitoring
svc/monitoring-kube-prometheus-prometheus 9090:9090` running in
another terminal)
"""

import sys

sys.path.insert(0, "p3_trust_action")

from actions import restart_deployment  # noqa: E402
from verifier import verify_durability  # noqa: E402

print("Applying fix...")
fix_result = restart_deployment("carts")
print(fix_result)

if not fix_result["applied"]:
    print("Fix did not apply -- skipping verification.")
    sys.exit(1)

print("Fix applied. Verifying it holds for the durability window (~2 min, polling every 15s)...")
verdict = verify_durability("crash-loop", "carts")
print(verdict)
