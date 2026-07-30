"""
Phase H step 5 (part 2): Gemini preview-model staleness guard.

Real reason this exists, not hypothetical (wardence_context.md's Model
Strategy section): Google's own deprecation history shows preview
models get roughly 4.5-8 months of life with a real, confirmed minimum
of only 2 WEEKS' notice before a hard shutdown. gemini-3-flash-preview
(the pinned model in model_backend.PROVIDER_CHAIN) could disappear with
very little warning. This is the "second, ongoing job" the locked
policy calls for, beyond a one-time check at wiring time -- run this
periodically (before a batch/roster run, or on some regular cadence),
not once and forgotten.

Queries Gemini's real /v1beta/models list endpoint directly -- does NOT
guess from docs or from this file's own past runs. Exits non-zero (and
prints a clear warning) if the pinned model is missing or the request
itself fails, so this can be used as a real pre-flight gate in a
future automated run, not just a manual curiosity check.

Usage:
    python3 check_gemini_staleness.py
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PINNED_MODEL = "gemini-3-flash-preview"


def main() -> int:
    key = os.environ["GEMINI_API_KEY"]
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", timeout=15,
        )
    except requests.exceptions.RequestException as e:
        print(f"FAILED to reach Gemini's real /models endpoint: {e}")
        return 1

    if resp.status_code != 200:
        print(f"FAILED: Gemini /models returned HTTP {resp.status_code}: {resp.text[:500]}")
        return 1

    real_model_names = {
        m["name"].removeprefix("models/") for m in resp.json().get("models", [])
    }

    if PINNED_MODEL in real_model_names:
        print(f"OK: {PINNED_MODEL!r} is still present in Gemini's real live model list.")
        return 0

    print(
        f"WARNING: {PINNED_MODEL!r} is NO LONGER in Gemini's real live model list -- "
        f"it has likely been deprecated/removed. model_backend.PROVIDER_CHAIN's Gemini "
        f"entry needs a manually-chosen replacement model ID before this provider is "
        f"trustworthy again. Real models currently available: {sorted(real_model_names)}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
