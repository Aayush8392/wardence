"""
Real check for a Cloudflare Workers AI model-serving change on
@cf/google/gemma-4-26b-a4b-it -- the free primary provider in
PROVIDER_CHAIN, which (confirmed live, diag_cloudflare_raw_live_check.py)
exposes no per-call version signal at all (no system_fingerprint on the
chat response, unlike Groq's real per-call value). Same real gap and same
fix shape as check_deepinfra_model_version.py's Nemotron check: an
external periodic check against Cloudflare's own model-listing endpoint,
which carries real fields the per-call response doesn't -- the model's
internal `id` (a fixed UUID; changes only if Cloudflare re-registers this
name as a genuinely different catalog entry), `tags` (empty today, but
this is where a real deprecation flag would appear), and `properties`
(context_window/function_calling/reasoning/vision/price -- any of these
changing means the actually-served model's real spec shifted).

Wired into run_batch_plan.py's startup alongside the DeepInfra check, so
it runs automatically once per batch. Can still be run standalone:
`python check_cloudflare_model_version.py`.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL_NAME = "@cf/google/gemma-4-26b-a4b-it"
LOG_PATH = Path(__file__).resolve().parent / "cloudflare_model_version_log.jsonl"


def _properties_dict(entry: dict) -> dict:
    return {p["property_id"]: p.get("value") for p in entry.get("properties", []) if "property_id" in p}


def check_and_log() -> bool:
    """Same contract as check_deepinfra_model_version.check_and_log():
    returns True if the check ran cleanly, False on a real network/API
    failure -- never a reason for the caller to abort a real batch."""
    try:
        key = os.environ["CLOUDFLARE_API_KEY"]
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        resp = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[cloudflare version check] SKIPPED -- real failure fetching models/search: {e}")
        return False

    results = data.get("result", data if isinstance(data, list) else [])
    found = None
    for m in results:
        if isinstance(m, dict) and m.get("name") == MODEL_NAME:
            found = m
            break

    if found is None:
        print(f"[cloudflare version check] WARNING: {MODEL_NAME} not found at all "
              "in Cloudflare's real models/search response -- possibly removed entirely.")
        snapshot = {"model_name": MODEL_NAME, "found": False}
    else:
        snapshot = {
            "model_name": MODEL_NAME, "found": True,
            "id": found.get("id"), "created_at": found.get("created_at"),
            "tags": found.get("tags"), "properties": _properties_dict(found),
        }

    snapshot["checked_at"] = datetime.now(timezone.utc).isoformat()

    last_snapshot = None
    if LOG_PATH.exists():
        lines = LOG_PATH.read_text().strip().splitlines()
        if lines:
            last_snapshot = json.loads(lines[-1])

    if last_snapshot is not None:
        changed = []
        for field in ["found", "id", "tags", "properties"]:
            if last_snapshot.get(field) != snapshot.get(field):
                changed.append((field, last_snapshot.get(field), snapshot.get(field)))
        if changed:
            print(f"[cloudflare version check] *** REAL CHANGE DETECTED since last check "
                  f"({last_snapshot.get('checked_at')}) ***")
            for field, old, new in changed:
                print(f"  {field}: {old!r} -> {new!r}")
            print("  Worth investigating before trusting this batch's Cloudflare/gemma results.")
        else:
            print(f"[cloudflare version check] No change since last check "
                  f"({last_snapshot.get('checked_at')}). Still: {snapshot}")
    else:
        print(f"[cloudflare version check] First recorded check. Baseline: {snapshot}")

    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    return True


if __name__ == "__main__":
    check_and_log()
