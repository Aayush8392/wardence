#!/usr/bin/env python3
"""Publish the current Cloudflare quick-tunnel URLs to R2 as runtime_config.json.

Why this exists: the operator API and storefront sit behind Cloudflare *quick*
tunnels (no account / no domain), whose hostname is re-randomised every time
cloudflared restarts (crash, auto-update, deploy). Baking the URL into Vercel's
build-time VITE_* env var means the frontend breaks ("failed to fetch") on every
churn until someone manually re-pastes the URL and redeploys.

Instead: this script reads the current URL each tunnel logged, writes both into
one small JSON in the R2 bucket the frontend already reads, and the frontend
picks them up at load time (src/api/runtimeConfig.js). A churn then self-heals
within one timer interval, no redeploy.

Driven by:
  - wardence-publish-endpoints.timer  -- every ~2 min (the safety net)
  - ExecStartPost on each tunnel unit -- ~15s after a restart (the fast path)

Idempotent: uploads only when a URL actually changed. Never overwrites a good
URL with a blank -- a tunnel whose URL can't be read keeps whatever is already
published.

R2 creds: p3_trust_action/.env, same four keys as publish_to_r2.py.
"""
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import boto3

ENV_PATH = Path(__file__).resolve().parent.parent / "p3_trust_action" / ".env"
KEY = "runtime_config.json"
URL_RE = re.compile(r"https://[a-z0-9-]{6,}\.trycloudflare\.com")

# field in runtime_config.json  ->  systemd unit that logs its URL
UNITS = {
    "operator_api_url": "wardence-tunnel-operator-api",
    "storefront_url": "wardence-tunnel-storefront",
}


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def current_url(unit: str) -> str | None:
    """The last trycloudflare URL this unit logged, or None if not readable."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--no-pager", "-o", "cat", "--since", "-14 days"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:  # noqa: BLE001 -- any failure here is non-fatal
        print(f"  journalctl {unit}: {e}", file=sys.stderr)
        return None
    hits = [m.group(0) for m in URL_RE.finditer(out) if "//api." not in m.group(0)]
    return hits[-1] if hits else None


def main() -> None:
    env = load_env(ENV_PATH)
    for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "R2_BUCKET_NAME"):
        if not env.get(k):
            print(f"FAILED: {k} missing/blank in {ENV_PATH}", file=sys.stderr)
            sys.exit(1)

    client = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = env["R2_BUCKET_NAME"]

    # Start from whatever is already published so a URL we can't read this run
    # is preserved rather than dropped.
    try:
        existing = json.loads(client.get_object(Bucket=bucket, Key=KEY)["Body"].read())
    except Exception:  # noqa: BLE001 -- first run: object doesn't exist yet
        existing = {}

    cfg = {k: v for k, v in existing.items() if k != "updated_at"}
    changed = False
    for field, unit in UNITS.items():
        url = current_url(unit)
        if url and url != cfg.get(field):
            print(f"  {unit}: {cfg.get(field)} -> {url}")
            cfg[field] = url
            changed = True
        elif not url and field not in cfg:
            print(f"  {unit}: no URL found and none published yet", file=sys.stderr)

    if not changed:
        print(f"unchanged: {cfg.get('operator_api_url')} / {cfg.get('storefront_url')}")
        return

    cfg["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = json.dumps(cfg, indent=2).encode("utf-8")
    client.put_object(
        Bucket=bucket,
        Key=KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="no-store, max-age=0",
    )
    print(f"published {KEY}: {cfg}")


if __name__ == "__main__":
    main()
