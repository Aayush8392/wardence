"""One-off script to confirm the wardence-agent SA is actually enforced.
Run: python3 p3_trust_action/test_cage.py  (from repo root)
"""

import sys

sys.path.insert(0, "p3_trust_action")

from actions import _apps_v1  # noqa: E402
from kubernetes import client  # noqa: E402

api = _apps_v1()

try:
    result = api.delete_namespaced_deployment(
        name="carts", namespace="sock-shop", dry_run="All"
    )
    print("UNEXPECTED SUCCESS -- cage is not enforced:")
    print(result)
except client.ApiException as e:
    print(f"Got {e.status}: {e.reason} -- ", end="")
    print("cage enforced correctly." if e.status == 403 else "unexpected status.")
