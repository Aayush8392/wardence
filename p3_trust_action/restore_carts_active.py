"""
crash-loop warm-standby backward flip -- the real bounded poll,
extracted into its own script so it can run as a genuinely DETACHED
background process (Model A, locked -- see
wardence_crash_loop_warm_standby_LOCKED_SPEC.md).

Spawned by p3_scorer.py via subprocess.Popen(..., start_new_session=True),
never awaited -- this is deliberate. carts' own real boot (measured:
min 266s, max 533s) routinely outlasts p3_scorer.py's own real runtime,
and operator_api.py's live-trigger path has a real, hard
SCORER_TIMEOUT_S=400 on the subprocess call that invokes p3_scorer.py.
Running this poll INLINE inside p3_scorer.py's own process (an earlier,
now-corrected version of this fix did exactly that) would make
p3_scorer.py's own subprocess frequently exceed 400s on a real slow
boot, causing operator_api.py to kill it as timed-out and wrongly mark
an already-successfully-scored episode as failed -- purely because of
this cosmetic restoration step, nothing to do with the real scoring
outcome. Detaching this into its own process, spawned and forgotten,
means p3_scorer.py's own runtime is completely unaffected by how long
carts actually takes to recover.

Same real bounded-ceiling discipline as everywhere else in this
project: gives up loudly after CEILING_S rather than polling forever.
If it gives up, the next crash-loop trigger's own
_ensure_crash_loop_baseline check (injector.py) keeps blocking new
injections until this is genuinely resolved -- never leaves the system
in a state where a fault could land on a target that isn't back to
steady state.

Usage: python3 restore_carts_active.py
(No arguments -- always restores the one real carts/carts-warm pair.)
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import carts_rotation  # noqa: E402

POLL_INTERVAL_S = 10
CEILING_S = 660  # real measured max boot (533s) + margin, same sizing discipline as every other per-class constant in this project


def main() -> None:
    deadline = time.time() + CEILING_S
    restored = carts_rotation.flip_to_carts()
    while not restored and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        restored = carts_rotation.flip_to_carts()

    if restored:
        print("carts restored to active -- steady state confirmed.")
    else:
        print(f"carts still not confirmed Ready after {CEILING_S}s -- giving up this "
              f"attempt. The next crash-loop trigger's own baseline check will keep "
              f"blocking (correctly) until this is resolved for real; check "
              f"`kubectl describe pod -n sock-shop -l name=carts` for the real cause.")


if __name__ == "__main__":
    main()
