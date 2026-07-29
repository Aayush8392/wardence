"""One-off script: confirms the DL/HMM/SPC fallback wiring in
agent.py's /diagnose (added 2026-07-29, see call_dl_detector's
docstring) actually overrides the diagnosis when the detector reports
an anomaly.

Real constraint this exists to work around: every one of the 5 services
with detector coverage already has its own Prometheus-based rule for its
mapped fault class(es), so today's real fault classes can never produce
a genuine "Prometheus sees nothing, but the logs are anomalous" case --
the exact condition this fallback is built for. Can't be exercised
organically without a genuinely novel/unlabeled fault, so this
monkeypatches ONLY call_dl_detector (forcing is_anomalous=True) to
confirm the real wiring/override logic, without touching
query_prometheus or fabricating any fake episode/DB data.

Requires a real kubectl port-forward to Prometheus already running --
query_prometheus itself is NOT mocked, only call_dl_detector is. Run
against a currently-healthy target so the real baseline call reports
"no anomaly detected" before the override is tested.

Run (from p2_readonly_loop/):
    python3 test_dl_detector_fallback_wiring.py
"""

import agent
from agent import DiagnoseRequest, diagnose

TARGET = "front-end"
NAMESPACE = "sock-shop"

print("-- baseline: real call, unpatched, confirms target is currently healthy --")
baseline = diagnose(DiagnoseRequest(target=TARGET, namespace=NAMESPACE))
print(baseline["diagnosis"])
assert baseline["diagnosis"] == "no anomaly detected", (
    f"expected a healthy baseline before testing the override -- got {baseline['diagnosis']!r}. "
    "pick a currently-healthy target, or wait for the current real fault (if any) to clear."
)

print("\n-- forcing call_dl_detector to report an anomaly --")
_real_call_dl_detector = agent.call_dl_detector
agent.call_dl_detector = lambda service: {
    "service": service,
    "track": "deeplog",
    "anomaly_score": 0.99,
    "threshold": 0.2,
    "is_anomalous": True,
    "events_used": 30,
}
try:
    result = diagnose(DiagnoseRequest(target=TARGET, namespace=NAMESPACE))
finally:
    agent.call_dl_detector = _real_call_dl_detector  # always restore, even on assertion failure

print(result["diagnosis"], result["confidence"])
assert result["diagnosis"] == "log-anomaly detected (unclassified)"
assert result["confidence"] == 0.5
assert result["tool_output"]["dl_detector_result"]["is_anomalous"] is True

print("\nALL ASSERTIONS PASSED -- DL/HMM/SPC fallback override wiring confirmed real.")
