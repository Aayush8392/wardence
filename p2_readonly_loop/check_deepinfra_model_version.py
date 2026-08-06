"""
Real check for a DeepInfra model-serving change on Nemotron-3-Nano-30B-A3B
-- the primary paid provider in PROVIDER_CHAIN, which (confirmed live,
diag_deepinfra_model_list_check.py) exposes no per-call version signal at
all (no system_fingerprint on the chat response). This is the external
substitute: DeepInfra's own dedicated /models/list endpoint carries real
fields a per-call response doesn't -- `deprecated`, `replaced_by`,
`quantization` -- any of which changing is a real, actionable signal that
the model being served may no longer be the one this project validated
against.

Wired into run_batch_plan.py's startup (alongside the existing baseline
check) so it runs automatically once per batch, not just on manual
request -- see check_and_log() below. Each call appends one timestamped
row to deepinfra_model_version_log.jsonl (gitignored, this project's real
historical record) and prints a loud warning if any tracked field differs
from the last recorded run. Real network failures (DeepInfra unreachable,
etc.) are swallowed and reported, not raised -- a real batch's episodes
must never abort over this being a non-critical side-check.

Can still be run standalone any time: `python check_deepinfra_model_version.py`.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL_NAME = "nvidia/Nemotron-3-Nano-30B-A3B"
LOG_PATH = Path(__file__).resolve().parent / "deepinfra_model_version_log.jsonl"
TRACKED_FIELDS = ["deprecated", "replaced_by", "quantization"]


def check_and_log() -> bool:
    """Returns True if the check ran cleanly (whether or not a change was
    found), False if it couldn't complete (network/API failure) -- the
    caller (run_batch_plan.py) should never treat a False here as a
    reason to abort a real batch, only log it and move on."""
    try:
        key = os.environ["DEEPINFRA_API_KEY"]
        resp = requests.get(
            "https://api.deepinfra.com/models/list",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[deepinfra version check] SKIPPED -- real failure fetching /models/list: {e}")
        return False

    entries = data if isinstance(data, list) else data.get("data", [])
    found = None
    for m in entries:
        if isinstance(m, dict) and m.get("model_name") == MODEL_NAME:
            found = m
            break

    if found is None:
        print(f"[deepinfra version check] WARNING: {MODEL_NAME} not found at all "
              "in DeepInfra's real /models/list response -- possibly removed entirely.")
        snapshot = {"model_name": MODEL_NAME, "found": False}
    else:
        snapshot = {"model_name": MODEL_NAME, "found": True, "create_ts": found.get("create_ts")}
        for field in TRACKED_FIELDS:
            snapshot[field] = found.get(field)

    snapshot["checked_at"] = datetime.now(timezone.utc).isoformat()

    last_snapshot = None
    if LOG_PATH.exists():
        lines = LOG_PATH.read_text().strip().splitlines()
        if lines:
            last_snapshot = json.loads(lines[-1])

    if last_snapshot is not None:
        changed = []
        for field in TRACKED_FIELDS + ["found"]:
            if last_snapshot.get(field) != snapshot.get(field):
                changed.append((field, last_snapshot.get(field), snapshot.get(field)))
        if changed:
            print(f"[deepinfra version check] *** REAL CHANGE DETECTED since last check "
                  f"({last_snapshot.get('checked_at')}) ***")
            for field, old, new in changed:
                print(f"  {field}: {old!r} -> {new!r}")
            print("  This is exactly the signal the drift-detection thread was built to catch -- "
                  "worth investigating before trusting this batch's Nemotron-tier results.")
        else:
            print(f"[deepinfra version check] No change since last check "
                  f"({last_snapshot.get('checked_at')}). Still: {snapshot}")
    else:
        print(f"[deepinfra version check] First recorded check. Baseline: {snapshot}")

    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    return True


if __name__ == "__main__":
    check_and_log()
