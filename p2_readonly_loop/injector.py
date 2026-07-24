"""
P2 injector: triggers a REAL fault against Sock Shop via Chaos Mesh and
records the ground-truth label (fault class, target, t0) into SQLite.

crash-loop: NOT Chaos Mesh either (as of this rewrite) -- originally
used a recurring Chaos Mesh Schedule running PodChaos/container-kill,
but chaos-daemon has a reproducible bug: it resolves and caches a
container's containerd task ID once, then never invalidates that cache
when the container's task actually changes (from a restart, whether
caused by chaos-daemon's own kill or anything else). The first kill
against a freshly-daemon-restarted target tends to succeed; as the
target's restart count accumulates since chaos-daemon was last
restarted, the odds of hitting the stale cache increase, producing
"no running task found: task ... not found" errors that get worse the
more the SAME target is used -- exactly what crash-loop's target
(carts) is, constantly, across a validation run. Restarting chaos-daemon
"fixes" it only temporarily. Confirmed with Kimi (reviews/03) that this
is a real architectural mismatch (Chaos Mesh treats container identity
as static for an experiment's lifetime; Kubernetes doesn't), not a
transient flake -- so this is driven directly instead: `kubectl exec`
SIGKILL (pkill -9 -f, NOT `kill -9 1` -- see _kill_main_process for why
targeting PID 1 empirically did NOT work here: carts' PID 1 is a shell
wrapper script that launches the real JVM as a child process, not via
`exec`, and killing PID 1 didn't tear the container down the way the
kernel's PID-namespace semantics would suggest it should; killing the
actual JVM process by name pattern is what was confirmed, manually, to
actually trigger a restart) the container's real application process,
repeated on an interval, letting kubelet restart it in place -- real,
accumulating restarts, genuine CrashLoopBackOff, same end effect as
container-kill, without chaos-daemon's broken cache in the loop at all.
Single pod-kill was tried first and rejected far
earlier in this project: it deletes the whole Pod object, so
Kubernetes' own controller silently recreates a fresh pod (0 restarts)
before the agent ever sees anything to diagnose or fix -- exec-kill
avoids that the same way container-kill did, by killing the process
IN PLACE rather than the pod.

oom: StressChaos memory stressor against catalogue (200Mi limit,
stressor requests 250Mi so the kubelet OOM-killer fires for real --
same failure signature as a genuine production OOM, not simulated).

The stressor sets oomScoreAdj: -1000. Without it, this was a coin-flip
that LOOKED reliable for a while: the kernel OOM-killer picks a victim
by oom_score within the cgroup, not "whoever caused the breach" -- the
stress-ng process itself (allocating 250M) usually has a HIGHER score
than catalogue's actual app, so the kernel was mostly killing the
stressor, not the app. Confirmed empirically (reviews/04):
container_oom_events_total=445 vs restarts=23 -- ~95% of OOM kills
were landing on the stressor, not catalogue. Protecting the stressor
with oomScoreAdj forces the kernel to kill the app instead, every time.

network-latency: Chaos Mesh NetworkChaos (delay action), direction "to"
against orders -- delays orders' own outbound traffic (i.e. its
responses), so every checkout request that depends on it gets
genuinely slower, not simulated. Blinding-safe the same way as the
other classes (CR lives in chaos-mesh namespace, no annotation on the
target pod itself).

Verification does NOT use Chaos Mesh's own state (that would be
reading the answer key) -- it uses the traffic generator's real,
independently-collected request latency. k6 (traffic_gen/) pushes
http_req_duration to Prometheus via its built-in
experimental-prometheus-rw output (no xk6 build needed, built in since
k6 v0.42.0); Prometheus must have --web.enable-remote-write-receiver
enabled (Helm: prometheus.prometheusSpec.enableRemoteWriteReceiver) or
the pushes are silently rejected. The injector compares p95 latency
for orders-bound requests just before injection to p95 during/after
it -- a real, externally-observed slowdown, the same "verify the
actual effect, not just that the API call succeeded" discipline used
everywhere else in this file.

disk-full: NOT Chaos Mesh -- this installation's StressChaos only
supports cpu/memory stressors (confirmed via `kubectl explain`), and
the CRDs that exist for I/O (iochaos, blockchaos) simulate faults
(delay/errors) rather than consuming real disk space. So this is
driven directly: `kubectl exec` into the target container, write a
real file past its ephemeral-storage limit, repeat.

Target is queue-master, not payment -- payment's root filesystem is
readOnlyRootFilesystem: true with only tmpfs (Memory-backed) mounts,
so nothing written there would ever count as real disk usage (it'd
silently stress memory instead, misrepresenting this as an oom fault).
queue-master has a writable, disk-backed /tmp (confirmed via exec) and
had no ephemeral-storage limit until patched
(patch_queue_master_ephemeral_limit.sh, 300Mi -- raised from the
original 100Mi on 2026-07-22, after repeated real evictions on a
freshly-fixed pod showed 100Mi had become too tight relative to
queue-master's real accumulated baseline usage after a long session).

Like crash-loop, this repeats on an interval rather than firing once:
an ephemeral-storage breach doesn't restart the container in place --
kubelet EVICTS the whole pod and the ReplicaSet creates a brand-new
pod object, the same trap pod-kill fell into for crash-loop. Each
cycle resolves the CURRENT pod (which may have changed since the last
cycle) and writes into it.

This is the ONLY place the true fault label is written. The agent must
never read this DB or see chaos-mesh.org resources (see blinding test,
P1 -- rerun the same style of check against any new fault mechanism
before trusting it for real episodes).

GROUND TRUTH IS ONLY RECORDED AFTER THE FAULT'S EFFECT IS VERIFIED, not
just after the k8s API accepted the request. Found the hard way: a
crash-loop episode's `kubectl apply` for its Schedule succeeded fine,
but chaos-daemon's own logs showed repeated "no running task found:
task ... not found" -- a stale containerd task reference inside Chaos
Mesh itself -- so the kill silently never executed underneath. The
restart count never moved, but without this fix the episode would have
been recorded as real ground truth anyway, corrupting the scorer, the
trust engine, and the (global) circuit breaker for a fault that never
actually happened -- none of that would be the agent's fault, but it
would have looked like one. Each class now: captures a baseline,
injects, polls for its own effect signal, and retries (re-injecting)
up to MAX_INJECT_ATTEMPTS before giving up. On total failure, NO
episode is recorded at all -- a loud warning prints instead, so a
consistently failing injector shows up as "no unscored episodes found"
rather than silently poisoning the data.

cpu-throttling: StressChaos cpu stressor (1 worker, 100% load) against
`user` -- reuses the exact same StressChaos primitive OOM already uses,
just the cpu stressor mode instead of memory. Verified via
container_cpu_cfs_throttled_periods_total, the only CFS-throttling
metric this cluster actually exposes (the more obvious
*_seconds_total variant returns zero series here -- confirmed via a
real query before writing any dependent code, same "assumed metric
isn't actually exposed" surprise disk-full's container_fs_usage_bytes
already taught). This counter is non-resetting and already nonzero
under light idle traffic (553 at measurement time), same shape as the
restart-count metrics -- so verification compares a raw delta
(after - baseline), never a raw/instant value. Real measurement
(2026-07-24, before any code was written): baseline delta over 60s of
no stress = 0.0; during a real stressor, delta over 30s ~= 300 (~600
projected per 60s) -- an enormous, unambiguous margin, not a guess.

Usage:
    python injector.py --class crash-loop
    python injector.py --class oom
    python injector.py --class disk-full
    python injector.py --class network-latency
    python injector.py --class memory-leak
    python injector.py --class connection-pool-exhaustion
    python injector.py --class network-partition
    python injector.py --class init-failure
    python injector.py --class session-cart-failure
    python injector.py --class cpu-throttling
    python injector.py --class under-provisioned-replicas
    python injector.py --class bad-rollout
"""

import argparse
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

PROMETHEUS_URL = "http://localhost:9090"
MAX_INJECT_ATTEMPTS = 3
EFFECT_VERIFY_TIMEOUT_S = 35  # covers kube-state-metrics' ~30s scrape cycle
EFFECT_VERIFY_POLL_S = 5

FAULT_CONFIG = {
    "crash-loop": {
        "namespace": "sock-shop",
        "target": "carts",
        "container": "carts",
        "duration_s": 40,
        "kill_pattern": "app.jar",
    },
    "oom": {
        "namespace": "sock-shop",
        "target": "catalogue",
        "container": "catalogue",
        "duration_s": 60,
        "chaos_name_prefix": "wardence-oom",
    },
    "disk-full": {
        "namespace": "sock-shop",
        "target": "queue-master",
        "container": "queue-master",
        "duration_s": 60,
    },
    "network-latency": {
        "namespace": "sock-shop",
        "target": "orders",
        "duration_s": 60,
        "chaos_name_prefix": "wardence-latency",
    },
    "memory-leak": {
        "namespace": "sock-shop",
        "target": "shipping",
        "container": "shipping",
        "duration_s": 100,
        "chaos_name_prefix": "wardence-memleak",
    },
    "connection-pool-exhaustion": {
        "namespace": "sock-shop",
        "target": "catalogue-db",
        "container": "catalogue-db",
        "duration_s": 60,
    },
    "network-partition": {
        "namespace": "sock-shop",
        "target": "orders",
        "duration_s": 60,
        "chaos_name_prefix": "wardence-partition",
    },
    "init-failure": {
        "namespace": "sock-shop",
        "target": "payment",
        "container": "payment",
        "duration_s": 60,
    },
    "session-cart-failure": {
        "namespace": "sock-shop",
        "target": "session-db",
        "duration_s": 60,
    },
    "under-provisioned-replicas": {
        "namespace": "sock-shop",
        "target": "catalogue",
        "duration_s": 20,
    },
    "bad-rollout": {
        "namespace": "sock-shop",
        "target": "front-end",
        "container": "front-end",
        "duration_s": 60,
    },
    "cpu-throttling": {
        "namespace": "sock-shop",
        "target": "user",
        "container": "user",
        "duration_s": 60,
        "chaos_name_prefix": "wardence-cputhrottle",
    },
}

CRASH_LOOP_KILL_INTERVAL_S = 10  # matches the original chaos-mesh cron cadence
OOM_STRESS_SIZE = "250M"  # catalogue's memory limit is 200Mi; stress-ng format, not Ki/Mi

# Found the hard way (2026-07-21): a real, successful oom fix
# (p3_trust_action's patch_memory_limit) permanently raises catalogue's
# memory limit to 400Mi (see p3_agent.py FIX_PARAMS["oom"]) -- nothing
# else ever reverts it. Without a reset, repeated real oom testing
# after one successful fix cycle silently stops reproducing the fault
# at all (the 250M stressor can never push memory over a 400Mi
# ceiling), which looked exactly like unrelated flakiness for a while
# before the real cause was found -- cost real debugging time chasing
# memory-pressure and chaos-daemon theories first. crash-loop and
# disk-full's own fixes (restart_deployment, restore_from_disk_full)
# don't have this problem -- they cycle the pod, they don't change any
# persistent config -- so this reset is oom-specific, not generic.
OOM_BASELINE_MEMORY_LIMIT = "200Mi"

DISK_FULL_FIRE_INTERVAL_S = 15  # wait between write attempts, giving kubelet time to detect+evict
DISK_STRESS_BYTES = 450_000_000  # queue-master's ephemeral-storage limit is 300Mi

NETWORK_LATENCY_DELAY = "500ms"
NETWORK_LATENCY_JITTER = "50ms"
# Well below the 500ms injected delay -- tolerates measurement noise
# while still requiring a real, unambiguous slowdown before ground
# truth is recorded.
NETWORK_LATENCY_MIN_INCREASE_MS = 200

# network-partition: NetworkChaos `partition` (direction=to) against
# orders. Empirically confirmed (2026-07-24, measurement scripts, not
# assumed) via a real 60s test with real CR-status checks: `loss`
# (100%) and `partition` are behaviorally identical -- both fully block
# orders' egress for the ENTIRE declared duration, no leakage. An
# earlier, shorter (20s) test looked "partial" (only ~1-2 of 5 probe
# samples failing) purely from probe-pod scheduling overhead eating
# into the fault window, not a real leaky block -- confirmed by
# widening to 60s and checking mid-window CR status directly
# (AllInjected=True held throughout). Verified via a direct probe (same
# pattern as _probe_orders_latency_ms, one throwaway pod running all
# samples in a loop -- NOT one pod per sample, for the same
# window-budget reason), not k6/Prometheus: front-end's own POST
# /orders call (api/orders/index.js, the `request` library) has NO
# client-side timeout at all, so it hangs indefinitely rather than ever
# producing a k6-observable failure while orders is unreachable --
# confirmed via front-end's real upstream source, not assumed. This
# rules out k6_http_req_failed as a usable signal for this class
# entirely (same category of lesson network-latency already taught
# once, for a different reason -- see NETWORK_LATENCY_MIN_INCREASE_MS's
# history).
NETWORK_PARTITION_PROBE_SAMPLES = 5
NETWORK_PARTITION_PROBE_TIMEOUT_S = 30
# Real measured behavior: a genuinely active partition produced 5/5
# hard failures every time it was checked mid-window. Requiring 4/5
# (not a strict 5/5) tolerates one stray sample landing right at CR
# application/teardown boundaries without weakening the real signal --
# mirrors NETWORK_LATENCY_MIN_INCREASE_MS's own "real margin, not exact
# equality" reasoning.
NETWORK_PARTITION_MIN_FAILURES = 4

# init-failure: not Chaos Mesh at all -- a direct kubectl patch of
# payment's readinessProbe.httpGet.path, leaving livenessProbe
# untouched (confirmed via real config, 2026-07-24: payment has
# separate readiness/liveness blocks, both pointed at the real
# /health). Patching ONLY readiness makes kubelet mark the pod
# permanently Ready=false without ever restarting it -- a genuine
# "stuck but running, never ready" signature, distinct from
# crash-loop's repeated-restart signature. Real numbers confirmed via
# the deployment's own live config before picking this mechanism:
# failureThreshold=3, periodSeconds=3, so the flip to Ready=false
# should land within ~9-12s of the patch landing.
SESSION_FAILURE_SCALE_TIMEOUT_S = 60  # matches disk-full's own hard-won lesson: 30s
# (default terminationGracePeriodSeconds) is too tight a margin, needed 60s there too.

# cpu-throttling: user's real CPU limit is 300m, real baseline usage
# ~9m under light traffic (both confirmed via direct Prometheus query,
# 2026-07-24, not assumed) -- enormous headroom, so a 1-worker 100%-load
# stressor overshoots the limit trivially and reliably.
CPU_THROTTLE_STRESS_WORKERS = 1
CPU_THROTTLE_STRESS_LOAD = 100  # percent
# container_cpu_cfs_throttled_periods_total is non-resetting and
# already nonzero under light idle traffic (553 at measurement time) --
# same shape as the restart-count metrics, so this compares a raw
# delta (after - baseline), never an instant/raw value. Real measured
# margin (2026-07-24): baseline delta over 60s = 0.0; during-stress
# delta over 30s ~= 300 (~600 projected per 60s). 50 leaves a huge,
# real margin on both sides -- not a guess.
CPU_THROTTLE_MIN_PERIODS_INCREASE = 50

# Same lesson OOM already taught the hard way (2026-07-21): a real
# successful patch_cpu_limit fix permanently raises user's CPU limit
# above this baseline, and nothing else ever reverts it -- without a
# reset, the SAME 100%-load stressor could stop reliably overshooting
# the (now-raised) limit after the first successful fix cycle, which
# looked like unrelated flakiness for OOM until the real cause was
# found. cpu-throttling gets the same _ensure_*_baseline treatment
# proactively this time, not after rediscovering the bug.
CPU_THROTTLE_BASELINE_CPU_LIMIT = "300m"

# under-provisioned-replicas: unlike every other class, the fault is a
# STANDING CONFIG STATE (catalogue stuck at 1 replica), not a
# transient injected condition -- as long as it stays at 1 replica,
# ANY sufficiently large real burst independently reveals the same
# degradation, whether fired by this injector, the agent's own later
# diagnosis probe, or a durability re-check. No Chaos Mesh resource,
# no persistent process to hold open between injection and diagnosis.
# Real numbers, confirmed across 3 independent trials + an interleaved
# 1->3->1->3 reproducibility check (2026-07-24, measure_catalogue_load.py
# + _confirm.py) before any of this code was written: p95 consistently
# 295-598ms at 1 replica, consistently 100-291ms at 3 replicas, ZERO
# errors in every trial. 200ms sits with real margin on both sides.
K6_IMAGE = "grafana/k6:latest"
UNDER_PROVISIONED_VUS = 20
UNDER_PROVISIONED_DURATION_S = 20
UNDER_PROVISIONED_MIN_P95_MS = 200
UNDER_PROVISIONED_BASELINE_REPLICAS = 1

# bad-rollout: NOT Chaos Mesh, NOT exec-based -- a direct kubectl patch
# of front-end's image to a nonexistent tag, simulating a real bad
# deploy. Real config confirmed before writing this (2026-07-25):
# real image is weaveworksdemos/front-end:0.3.12, readinessProbe delay
# is 30s -- irrelevant here, since a nonexistent image means the
# container never starts at all (no image = no container = readiness
# never even gets a chance to run), so ImagePullBackOff/ErrImagePull
# should appear within seconds, not needing any probe-delay wait
# (genuinely simpler timing than init-failure's own mechanism). Same
# "old healthy pod keeps serving, new broken one never comes online"
# realism as init-failure -- confirmed by the same RollingUpdate
# mechanics already proven there.
FRONT_END_IMAGE_BASELINE = "weaveworksdemos/front-end:0.3.12"
FRONT_END_IMAGE_FAULT = "weaveworksdemos/front-end:0.3.12-wardence-badtag"


def _parse_k6_p95_ms(stdout_text: str) -> float | None:
    """Parses the real p95 value directly from k6's own end-of-run
    text summary -- found the hard way (2026-07-24) that
    `--no-summary`/`--summary-export=/dev/stdout` isn't a real flag on
    this k6 image (`unknown flag: --no-summary`, confirmed via a real
    failed run, not assumed), so JSON export isn't available here.
    Looks for the http_req_duration line specifically (not
    iteration_duration, which reports a near-identical but distinct
    metric), extracts the value+unit after 'p(95)=', and converts to
    ms. Returns None if the line/pattern isn't found."""
    match = re.search(
        r"http_req_duration[^\n]*p\(95\)=([\d.]+)(µs|ms|s)\b", stdout_text
    )
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2)
    if unit == "s":
        return value * 1000
    if unit == "µs":
        return value / 1000
    return value


def _catalogue_burst_p95_ms(namespace: str, vus: int, duration_s: int, label: str) -> float | None:
    """Fires a real k6 burst directly against catalogue's own Service
    (GET /catalogue?size=10, the same real endpoint validated during
    measurement) and returns the REAL p95, parsed from k6's own text
    summary via _parse_k6_p95_ms. Returns None if the probe pod
    couldn't run or its summary couldn't be parsed -- treated as
    'can't verify' by callers, matching every other probe-based
    helper's convention in this file."""
    pod_name = f"wardence-underprov-{label}-{uuid.uuid4().hex[:8]}"
    script = f"""
import http from 'k6/http';
export const options = {{
  scenarios: {{
    burst: {{
      executor: 'constant-vus',
      vus: {vus},
      duration: '{duration_s}s',
    }},
  }},
}};
export default function () {{
  http.get('http://catalogue.{namespace}.svc.cluster.local/catalogue?size=10');
}}
"""
    try:
        result = subprocess.run(
            [
                "kubectl", "run", pod_name, "--rm", "-i", "--restart=Never",
                "-n", namespace, f"--image={K6_IMAGE}",
                "--", "run", "--quiet", "-",
            ],
            input=script, capture_output=True, text=True, timeout=duration_s + 60,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return _parse_k6_p95_ms(result.stdout)


PAYMENT_READINESS_PATH_BASELINE = "/health"
PAYMENT_READINESS_PATH_FAULT = "/wardence-fault-nonexistent"
# Real mechanism confirmed empirically (2026-07-24, not assumed): this
# patch triggers a REAL RollingUpdate -- a new ReplicaSet's pod is
# created, but since it can never pass the broken readiness probe, the
# OLD pod's ReplicaSet never scales down. The OLD pod keeps serving
# traffic the ENTIRE time (confirmed: same pod name/age, untouched) --
# matches how a real bad-readiness-probe deploy behaves in production
# (old healthy replicas keep serving, new broken ones never come
# online, no full outage). Verification uses EFFECT_VERIFY_TIMEOUT_S/
# EFFECT_VERIFY_POLL_S (the same kube-state-metrics-scrape-cycle-aware
# constants every other class's Prometheus-based verification already
# uses), not a separate constant -- an initial guess here (9-12s based
# on the probe's failureThreshold/periodSeconds) was WRONG: that timing
# only applies to an ALREADY-Ready pod transitioning to NotReady, not a
# brand-new pod, which starts NotReady immediately by k8s's own default
# (Ready is false until the first successful probe) -- caught before
# writing dependent code, not after a failed run.

# shipping's container memory LIMIT is 500Mi (its JAVA_OPTS -Xmx128m is
# an internal JVM heap cap, irrelevant here -- StressChaos's stress-ng
# process runs OUTSIDE the JVM but inside the same cgroup, so it
# consumes memory against the 500Mi container limit regardless of the
# app's own heap setting, same mechanism already proven for oom's
# catalogue target).
#
# Found the hard way BEFORE running anything (checked first, per this
# session's own established discipline): shipping's baseline working
# set is already ~298MiB (confirmed via a direct Prometheus query),
# not near-zero as first assumed -- leaving only ~202MiB of real
# headroom under the 500Mi limit. The originally planned 300M stressor
# would have pushed total usage to ~598MiB, well past the limit,
# causing a real OOM kill instead of the sustained "elevated but never
# restarts" signature this class is supposed to have. Sized down to
# fit comfortably within the real headroom instead.
MEMORY_LEAK_STRESS_SIZE = "150M"
MEMORY_LEAK_MIN_INCREASE_MIB = 100

# catalogue-db's max_connections is 151 (confirmed still unchanged,
# 2026-07-25). baseline Threads_connected was ~2-3 on 2026-07-21;
# re-checked 2026-07-25 (real drift after days of accumulated testing)
# and found at 7 -- real growth, re-measured rather than assumed still
# true.
#
# Found the hard way (2026-07-21): first tried flooding 140, reasoning
# it "leaves ~11 connections of headroom" -- backwards. Confirmed via
# a manual test (Threads_connected reached 143/151, a genuine new
# connection still succeeded with 8 slots free) that leaving headroom
# is exactly the OPPOSITE of what "exhaustion" needs -- unlike
# memory-leak, where staying under the limit was the whole point, this
# class needs the pool actually FULL. Bumped to 150 so
# baseline(~3) + flood pushes past 151 -- some of the flood's own 150
# connection attempts may themselves fail once the real ceiling is
# hit, which is expected and fine (that's the ceiling working, not a
# bug), as long as enough land to genuinely fill the pool.
#
# Found the hard way AGAIN (2026-07-25, during under-provisioned-
# replicas' Phase 2 cross-check): 150 stopped being enough real margin.
# Direct diagnostic (measure_connection_pool_flood_reliability.py)
# confirmed real exhaustion WAS reached (Threads_connected hit exactly
# 151, held for ~24s) but a fresh catalogue_user test connection still
# slipped in -- because only 144 of the 150 targeted flood connections
# actually landed (6 failed to establish, a real MySQL boundary-
# condition effect, not a bug), and the real baseline growth (~3 -> 7)
# further thinned the already-intentionally-tight margin. Bumped to
# 170 to rebuild real headroom given the drifted baseline -- not a
# design flaw, a real number that needed re-measuring and adjusting,
# same as every other real threshold in this project.
CONNECTION_POOL_FLOOD_CONNECTIONS = 170

# Found the hard way (2026-07-21): the flood originally used root for
# every connection -- the SAME user mysqld_exporter uses for its own
# scrape. MySQL reserves a small number of extra connection slots for
# privileged (SUPER) users specifically so an admin can still log in
# during real exhaustion; since our flood was also root, it could eat
# that reserved slot too, starving the exporter's own scrape
# connection right alongside everything else. Confirmed empirically:
# during a real, verified exhaustion, mysql_global_status_threads_connected
# stayed flat at baseline the ENTIRE time -- the metric that was
# supposed to observe the fault was itself a casualty of it.
# Fixed with a separate, unprivileged user for the flood (created via
# create_connection_pool_flood_user.sh -- SELECT SLEEP() needs no
# table access, so USAGE is enough), leaving root exclusively for the
# exporter.
CONNECTION_POOL_FLOOD_USER = "floodtest"
CONNECTION_POOL_FLOOD_PASSWORD = "floodpass"

# The verification test connection must NOT use root either, for the
# same reason -- root would likely succeed via MySQL's reserved-slot
# mechanism even during real exhaustion, which would falsely look like
# "not exhausted." Uses catalogue's own actual DSN credentials instead
# (confirmed via the app's real source, microservices-demo/catalogue's
# cmd/cataloguesvc/main.go default -DSN flag, and verified this account
# actually works against catalogue-db) -- the realistic account that
# should genuinely fail, since it's the same one real user traffic
# depends on.
CONNECTION_POOL_TEST_USER = "catalogue_user"
CONNECTION_POOL_TEST_PASSWORD = "default_password"

# ABANDONED (2026-07-21): originally verified via k6's own
# k6_http_req_duration_p95 (pushed to Prometheus via experimental-
# prometheus-rw). Empirically confirmed this stat is a slow-converging
# reservoir/streaming-percentile estimator, not a simple recent time
# window: after a real 500ms-delay episode ended, repeated direct
# queries showed it decaying 2.08s -> 2.08s -> 1.79s -> 0.66s -> 0.19s
# -> ... over TWO MINUTES before approaching the true ~10-50ms
# baseline (confirmed via a direct curl timing test showing real
# latency was fine the whole time). A 30s settle wait (tried first)
# was nowhere near enough. Waiting minutes per retry attempt just to
# get a clean baseline isn't a workable verification mechanism -- so,
# same move as crash-loop (bypassed Chaos Mesh's broken task-ID cache)
# and disk-full (bypassed Chaos Mesh's nonexistent real I/O stressor):
# stop trusting the laggy proxy, measure the real effect directly.
# k6/Prometheus's http_req_duration stays wired up for the traffic-gen
# dashboard (P4) -- just not trusted for THIS ground-truth decision.
LATENCY_PROBE_SAMPLES = 5
LATENCY_PROBE_IMAGE = "curlimages/curl"
LATENCY_PROBE_TIMEOUT_S = 30

# DB lives on WSL2's native filesystem, not the Windows-mounted C:/ path --
# DrvFs (WSL2's NTFS translation layer) has known SQLite file-locking bugs
# that caused hangs/"unable to open database file" when scripts hammered
# a DB on /mnt/c.
OUTPUT_DIR = Path.home() / "wardence_p2_data"
DB_PATH = OUTPUT_DIR / "wardence.db"


def ensure_db():
    OUTPUT_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            episode_id TEXT PRIMARY KEY,
            fault_class TEXT NOT NULL,
            target TEXT NOT NULL,
            namespace TEXT NOT NULL,
            t0 TEXT NOT NULL,
            chaos_resource_name TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def build_oom_manifest(chaos_name: str, cfg: dict, size: str = OOM_STRESS_SIZE) -> str:
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: {chaos_name}
  namespace: chaos-mesh
spec:
  mode: one
  containerNames:
    - {cfg['container']}
  selector:
    namespaces:
      - {cfg['namespace']}
    labelSelectors:
      name: {cfg['target']}
  stressors:
    memory:
      workers: 1
      size: "{size}"
      oomScoreAdj: -1000
"""


def build_memory_leak_manifest(chaos_name: str, cfg: dict, size: str = MEMORY_LEAK_STRESS_SIZE) -> str:
    """No oomScoreAdj, unlike build_oom_manifest -- staying comfortably
    under the container's memory limit by design, so there's no OOM
    kill to steer away from the stressor here."""
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: {chaos_name}
  namespace: chaos-mesh
spec:
  mode: one
  containerNames:
    - {cfg['container']}
  selector:
    namespaces:
      - {cfg['namespace']}
    labelSelectors:
      name: {cfg['target']}
  stressors:
    memory:
      workers: 1
      size: "{size}"
"""


def build_cpu_throttle_manifest(chaos_name: str, cfg: dict) -> str:
    """Same StressChaos primitive OOM's build_oom_manifest already uses,
    just the cpu stressor mode instead of memory -- no oomScoreAdj
    needed here, there's no OOM-kill victim-selection problem for a CPU
    stressor."""
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: {chaos_name}
  namespace: chaos-mesh
spec:
  mode: one
  containerNames:
    - {cfg['container']}
  selector:
    namespaces:
      - {cfg['namespace']}
    labelSelectors:
      name: {cfg['target']}
  stressors:
    cpu:
      workers: {CPU_THROTTLE_STRESS_WORKERS}
      load: {CPU_THROTTLE_STRESS_LOAD}
"""


def build_network_latency_manifest(chaos_name: str, cfg: dict) -> str:
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {chaos_name}
  namespace: chaos-mesh
spec:
  action: delay
  mode: one
  selector:
    namespaces:
      - {cfg['namespace']}
    labelSelectors:
      name: {cfg['target']}
  delay:
    latency: "{NETWORK_LATENCY_DELAY}"
    jitter: "{NETWORK_LATENCY_JITTER}"
    correlation: "25"
  direction: to
  duration: "{cfg['duration_s']}s"
"""


def build_network_partition_manifest(chaos_name: str, cfg: dict) -> str:
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {chaos_name}
  namespace: chaos-mesh
spec:
  action: partition
  mode: one
  selector:
    namespaces:
      - {cfg['namespace']}
    labelSelectors:
      name: {cfg['target']}
  direction: to
  duration: "{cfg['duration_s']}s"
"""


def _probe_orders_reachable(namespace: str) -> int:
    """Runs NETWORK_PARTITION_PROBE_SAMPLES direct GET /health requests
    against orders' own Service, ALL from inside ONE throwaway pod via a
    shell loop (same pattern as _probe_orders_latency_ms, and for the
    same reason -- a fresh pod per sample burns real scheduling/
    image-pull overhead that can spill a short probe sequence past a
    short fault window, confirmed the hard way while measuring this
    class's own real behavior, 2026-07-24).

    Returns the number of samples that came back as a genuine failure
    (curl timeout/connection error, --max-time 5s), out of
    NETWORK_PARTITION_PROBE_SAMPLES attempted. Returns 0 if the probe
    pod itself couldn't run at all (image pull failure, scheduling
    issue) -- treated as "can't verify" by callers, matching
    _probe_orders_latency_ms's own None-on-failure convention adapted
    to a count."""
    pod_name = f"wardence-partition-probe-{uuid.uuid4().hex[:8]}"
    script = (
        f"for i in $(seq 1 {NETWORK_PARTITION_PROBE_SAMPLES}); do "
        f'curl -s -o /dev/null -w "HTTP_%{{http_code}}\\n" --max-time 5 '
        f"http://orders.{namespace}.svc.cluster.local/health; done"
    )
    try:
        result = subprocess.run(
            [
                "kubectl", "run", pod_name, "--rm", "-i", "--restart=Never",
                "-n", namespace, f"--image={LATENCY_PROBE_IMAGE}",
                "--", "sh", "-c", script,
            ],
            capture_output=True, text=True, timeout=NETWORK_PARTITION_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result = None
    finally:
        subprocess.run(
            ["kubectl", "delete", "pod", pod_name, "-n", namespace, "--ignore-not-found"],
            capture_output=True, text=True,
        )

    if result is None:
        return 0
    lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip().startswith("HTTP_")]
    if not lines:
        return 0
    return sum(1 for ln in lines if ln != "HTTP_200")


def _probe_orders_latency_ms(namespace: str) -> float | None:
    """Direct, timed HTTP requests via a throwaway pod -- see the
    ABANDONED note above NETWORK_LATENCY_MIN_INCREASE_MS for why this
    replaced a k6/Prometheus-metric-based check. Same pattern already
    confirmed trustworthy during this session's own debugging (a
    one-off curltest pod gave an accurate 10ms reading when the k6
    metric was falsely showing 2000ms+).

    Hits orders' OWN Service directly (GET /health, confirmed 200 via
    manual test), NOT front-end's /orders -- found the hard way that
    an unauthenticated probe through front-end never actually reached
    the orders microservice at all: front-end's order-building flow
    requires a real session (customer/address/card, per
    api/orders/index.js) and rejects a session-less request in ~7ms,
    well before ever calling orders' backend, so the injected delay on
    orders' egress was never being exercised. /health still traverses
    the same delayed egress path on its response.

    Runs LATENCY_PROBE_SAMPLES real GETs in one throwaway pod and
    returns the MAX (conservative -- one genuinely slow request is
    enough to confirm the delay landed). Returns None if the probe
    itself couldn't run (image pull failure, pod scheduling issue,
    etc.) -- treated as "can't verify" by callers, never as "zero
    latency"."""
    pod_name = f"wardence-latency-probe-{uuid.uuid4().hex[:8]}"
    script = (
        f"for i in $(seq 1 {LATENCY_PROBE_SAMPLES}); do "
        f'curl -s -o /dev/null -w "%{{time_total}}\\n" '
        f"http://orders.{namespace}.svc.cluster.local/health; done"
    )
    try:
        result = subprocess.run(
            [
                "kubectl", "run", pod_name, "--rm", "-i", "--restart=Never",
                "-n", namespace, f"--image={LATENCY_PROBE_IMAGE}",
                "--", "sh", "-c", script,
            ],
            capture_output=True, text=True, timeout=LATENCY_PROBE_TIMEOUT_S,
        )
    finally:
        # --rm should already have deleted it, but a subprocess timeout
        # kills our end of the connection, not necessarily the pod --
        # best-effort cleanup so a probe failure can't leave junk pods
        # accumulating in sock-shop (the app namespace the blinding
        # test guards).
        subprocess.run(
            ["kubectl", "delete", "pod", pod_name, "-n", namespace, "--ignore-not-found"],
            capture_output=True, text=True,
        )

    samples_s = []
    for line in result.stdout.strip().splitlines():
        try:
            samples_s.append(float(line.strip()))
        except ValueError:
            continue
    if not samples_s:
        return None
    return max(samples_s) * 1000


def _current_pod_name(target: str, namespace: str) -> str | None:
    """
    Found the hard way (reviews/04 follow-up): with no phase filter,
    `items[0]` is whatever the API happens to return first -- NOT
    guaranteed to be the healthy pod. Old evicted/errored pod objects
    from earlier episodes don't disappear immediately, so after enough
    testing on a target (queue-master had 2 dead `Error` pods sitting
    alongside the 1 live one), this was silently resolving to a DEAD
    pod. `kubectl exec` against it fails every time with no visible
    error in this function (subprocess.run below swallows it, matching
    every other caller's best-effort pattern) -- which is exactly what
    made disk-full's writes silently never land while looking, from the
    caller's side, like nothing was wrong. verifier.py's own
    _current_pod_name (a separate implementation) already filtered for
    phase=Running via a Prometheus query; this one never got the same
    protection until now.
    """
    result = subprocess.run(
        [
            "kubectl", "get", "pods", "-n", namespace,
            "-l", f"name={target}",
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    name = result.stdout.strip()
    return name or None


def _write_large_file(pod_name: str, namespace: str, container: str):
    # best-effort: the pod may get evicted mid-write, or the write may
    # fail once the limit is already breached -- both are fine, the
    # eviction is the actual fault signal we want, not this exec call
    # succeeding cleanly.
    subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "sh", "-c",
            f"head -c {DISK_STRESS_BYTES} /dev/zero > /tmp/wardence_fill_$$",
        ],
        capture_output=True,
        text=True,
    )


def _kill_main_process(pod_name: str, namespace: str, container: str, kill_pattern: str) -> bool:
    """
    Returns True if pkill actually matched and signaled a process, False
    if it found nothing to kill. NOT `kill -9 1` -- empirically confirmed
    wrong for carts specifically: `ps aux` showed PID 1 is a shell
    wrapper (java.sh) that launches the real JVM as a CHILD process, not
    via `exec`. In theory the kernel should still tear down the whole
    PID namespace when PID 1 dies, but that didn't happen here (verified
    manually: `kill -9 1` returned exit 0, restart count never moved,
    pod never blipped). Killing the actual process by name pattern
    instead -- confirmed this DOES trigger a real, immediate restart.
    -9 (SIGKILL) is deliberate: a graceful SIGTERM would let the app
    shut down cleanly (reason "Completed"), a real, different signal
    from what a genuine crash produces (reason "Error").

    The return value matters (found the hard way, per reviews/04): a
    silently-ignored return code means a broken kill_pattern (e.g. after
    some future image change) looks identical to "container was
    transiently down between kills" -- both produce silent no-ops and
    eventually just "no restart detected" with no clue why. pkill's exit
    code 1 specifically means "no process matched," which is worth a
    loud print if it happens (though NOT worth raising/aborting the
    loop on a single miss -- the container legitimately isn't always
    running between our own kills, that's expected and fine).
    """
    result = subprocess.run(
        ["kubectl", "exec", "-n", namespace, pod_name, "-c", container, "--", "pkill", "-9", "-f", kill_pattern],
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        print(f"    (pkill found no process matching '{kill_pattern}' in {pod_name} "
              f"-- may be mid-restart, or the pattern may be stale)")
        return False
    return result.returncode == 0


def run_crash_loop_injection(cfg: dict, duration_s: int):
    """
    Repeatedly SIGKILLs the container's real application process (by
    name pattern, NOT PID 1 -- see _kill_main_process) via kubectl exec
    on the SAME pod (in-place restart, same as the old container-kill
    action), so restarts genuinely accumulate and kubelet can back off
    into CrashLoopBackOff. Not a Chaos Mesh resource -- see module
    docstring for why.
    """
    end_time = time.time() + duration_s
    while time.time() < end_time:
        pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
        if pod_name is None:
            time.sleep(2)
            continue
        _kill_main_process(pod_name, cfg["namespace"], cfg["container"], cfg["kill_pattern"])
        time.sleep(CRASH_LOOP_KILL_INTERVAL_S)


def _restart_count(target: str, namespace: str) -> int:
    """Prefix-match sum is fine here -- called only as a pre-injection
    baseline, before anything has happened to create ambiguity between
    an old and new pod."""
    query = f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod=~"{target}.*"}}'
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    return sum(int(float(entry["value"][1])) for entry in result)


def _memory_working_set_mib(target: str, namespace: str, container: str) -> float | None:
    """cAdvisor's own container_memory_working_set_bytes -- a real,
    reliable kube-native cgroup metric (same trust tier as
    kube_pod_container_status_restarts_total), NOT the k6/Prometheus
    metric that turned out too unreliable for network-latency's ground
    truth (this one reflects true current kernel-reported memory, not
    an internal percentile estimator). Filtered on container= to avoid
    picking up the pause container's negligible footprint. Returns None
    if no data point exists yet, treated as "can't verify" by callers."""
    query = (
        f'container_memory_working_set_bytes{{namespace="{namespace}", '
        f'pod=~"{target}.*", container="{container}"}}'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    return max(float(entry["value"][1]) for entry in result) / (1024 * 1024)


def _cfs_throttled_periods(target: str, namespace: str, container: str) -> int:
    """Raw (non-resetting) counter, summed across matched series -- same
    convention as _restart_count. Confirmed via direct measurement
    (2026-07-24) that this counter is already nonzero under light idle
    traffic, so callers must always compare a delta against their own
    baseline snapshot, never a raw/instant value alone."""
    query = (
        f'container_cpu_cfs_throttled_periods_total{{namespace="{namespace}", '
        f'pod=~"{target}.*", container="{container}"}}'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    return sum(int(float(entry["value"][1])) for entry in result)


def _crash_loop_backoff_now(target: str, namespace: str) -> bool:
    """
    Found the hard way: after enough repeated crash-loop testing across
    a long session, kubelet's exponential backoff delay between restarts
    can grow well past our detection window (confirmed empirically: an
    ~8 minute gap between one restart and the next, vs. a ~75s window).
    Restart-count-increase alone can't see a fault that's real but whose
    NEXT restart kubelet is deliberately delaying. Being in
    CrashLoopBackOff right now is itself valid, standing evidence the
    fault is active -- it doesn't require catching the next tick. This
    mirrors agent.py's own crash_query, which already checks this same
    signal for diagnosis; the injector's self-verification was missing
    the other half of that same OR.
    """
    query = (
        f'kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="CrashLoopBackOff"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _verify_crash_loop_effect(target: str, namespace: str, baseline_restarts: int) -> bool:
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _restart_count(target, namespace) > baseline_restarts:
            return True
        if _crash_loop_backoff_now(target, namespace):
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _pod_evicted_since(target: str, namespace: str, since_ts: float) -> bool:
    """
    Bounded to THIS episode's own start time, not a fixed window --
    more precise than agent.py's 3-minute heuristic since the injector
    knows exactly when it began. Without this bound, an old evicted pod
    lingering from a PREVIOUS episode would make the injector think
    THIS episode's injection succeeded when it didn't.
    """
    query = (
        f'(kube_pod_status_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="Evicted"}} == 1) '
        f'and on(namespace, pod) (kube_pod_deletion_timestamp'
        f'{{namespace="{namespace}", pod=~"{target}.*"}} > {since_ts})'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _cleanup_disk_full_files(target: str, namespace: str, container: str):
    """
    Best-effort cleanup after every disk-full attempt, success or
    failure. If eviction never fired, the written files would otherwise
    sit there and poison the NEXT episode on this target -- including a
    genuine no-fault control. Idempotent: if the pod was evicted, the
    new pod has no files to delete and this is just a no-op 404.
    """
    pod_name = _current_pod_name(target, namespace)
    if pod_name is None:
        return
    subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "sh", "-c", "rm -f /tmp/wardence_fill_*",
        ],
        capture_output=True,
        text=True,
    )


def _verify_restart_effect(target: str, namespace: str, baseline_restarts: int) -> bool:
    """Used by both crash-loop and oom -- both classes' real signal is
    simply "did this pod restart since baseline," regardless of exact
    cause. Polls briefly to give kube-state-metrics' scrape cycle a
    chance to catch up rather than judging on a single instant."""
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _restart_count(target, namespace) > baseline_restarts:
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _verify_disk_full_effect(
    target: str, namespace: str, since_ts: float, baseline_pod_name: str | None
) -> bool:
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _pod_evicted_since(target, namespace, since_ts):
            return True
        current_pod_name = _current_pod_name(target, namespace)
        if current_pod_name is not None and current_pod_name != baseline_pod_name:
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def run_disk_full_injection(
    cfg: dict, duration_s: int, baseline_pod_name: str | None = None, since_ts: float | None = None
):
    """
    Repeatedly resolves the CURRENT pod (which changes identity each
    time kubelet evicts and the ReplicaSet recreates it) and writes a
    file past the ephemeral-storage limit into it. Not a Chaos Mesh
    resource -- see module docstring for why.

    Stops as soon as ONE real eviction is confirmed (via
    baseline_pod_name/since_ts, the same signals _verify_disk_full_effect
    checks separately), rather than always running the full duration_s.
    Found 2026-07-22: continuing to fill after the first eviction was
    also filling the freshly-created REPLACEMENT pod -- exactly the pod
    the later fix (restore_from_disk_full) acts on. That contaminated
    the fix's own target before the fix ever ran, causing a second,
    independent eviction that the trust ladder's durability check
    correctly (but confusingly) read as a fresh post-fix flap. One
    confirmed eviction is already sufficient proof of the fault --
    unlike network-latency, where stopping early starved the traffic
    generator of a chance to ever observe the fault, disk-full's proof
    doesn't depend on continued injection once eviction is confirmed.
    baseline_pod_name/since_ts are optional so this can still be called
    standalone (e.g. from a test script) without early-stop behavior.
    """
    end_time = time.time() + duration_s
    while time.time() < end_time:
        if baseline_pod_name is not None:
            # Check BEFORE writing, and only ever write to the ORIGINAL
            # baseline pod. The first version checked AFTER the write and
            # re-resolved the current pod each pass, which meant: write to
            # POD_A -> sleep -> kubelet evicts POD_A and creates POD_B ->
            # next pass re-resolves to POD_B and writes a full 450MB into
            # the brand-new REPLACEMENT pod -> only then notices the pod
            # changed and stops. That guaranteed the replacement pod was
            # already contaminated before the fix ever ran, and its later
            # eviction cascaded into the durability window, where the
            # verifier correctly reported real churn -- making a working
            # fix look flapped every single time (found 2026-07-22).
            # Once the baseline pod is gone, the fault has landed; there
            # is nothing more to inject.
            current_pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
            if current_pod_name is None or current_pod_name != baseline_pod_name:
                return
            if since_ts is not None and _pod_evicted_since(
                cfg["target"], cfg["namespace"], since_ts
            ):
                return
            pod_name = baseline_pod_name
        else:
            # Standalone call (no baseline given) -- original behaviour,
            # re-resolve each pass and run the full duration.
            pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
            if pod_name is None:
                time.sleep(2)
                continue
        _write_large_file(pod_name, cfg["namespace"], cfg["container"])
        time.sleep(DISK_FULL_FIRE_INTERVAL_S)


def apply_manifest(manifest: str):
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl apply failed:\n{result.stderr}")
    print(result.stdout.strip())


def delete_chaos_resource(kind: str, name: str):
    result = subprocess.run(
        ["kubectl", "delete", kind, name, "-n", "chaos-mesh"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: failed to delete {kind} {name}:\n{result.stderr}")
    else:
        print(result.stdout.strip())


def _ensure_oom_baseline(cfg: dict):
    """Resets catalogue's memory limit back to OOM_BASELINE_MEMORY_LIMIT
    before injecting, if it's currently anything else -- see the
    constant's docstring for why this is needed (a real successful fix
    permanently raises the limit, and nothing else reverts it).
    Idempotent: does nothing if the limit is already at baseline, which
    is the common case (only matters right after a real fix cycle)."""
    result = subprocess.run(
        [
            "kubectl", "get", "deployment", cfg["target"], "-n", cfg["namespace"],
            "-o", "jsonpath={.spec.template.spec.containers[0].resources.limits.memory}",
        ],
        capture_output=True, text=True,
    )
    current_limit = result.stdout.strip()
    if current_limit == OOM_BASELINE_MEMORY_LIMIT:
        return

    print(f"  {cfg['target']}'s memory limit is {current_limit or '(unknown)'}, not the "
          f"{OOM_BASELINE_MEMORY_LIMIT} baseline -- resetting before injecting "
          f"(a prior real fix likely raised it)...")
    patch_body = (
        '{"spec":{"template":{"spec":{"containers":[{"name":"' + cfg["container"] + '",'
        '"resources":{"limits":{"memory":"' + OOM_BASELINE_MEMORY_LIMIT + '"}}}]}}}}'
    )
    subprocess.run(
        [
            "kubectl", "patch", "deployment", cfg["target"], "-n", cfg["namespace"],
            "--type=strategic", "-p", patch_body,
        ],
        capture_output=True, text=True,
    )
    # Wait for the rollout to actually finish before injecting --
    # otherwise the stressor could target the OLD pod (still on the
    # non-baseline limit) while it's mid-termination.
    subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=180s",
        ],
        capture_output=True, text=True,
    )


def _ensure_cpu_throttle_baseline(cfg: dict):
    """Resets user's CPU limit back to CPU_THROTTLE_BASELINE_CPU_LIMIT
    before injecting, if it's currently anything else -- mirrors
    _ensure_oom_baseline's pattern exactly, same reason: a real
    successful patch_cpu_limit fix permanently raises the limit and
    nothing else reverts it."""
    result = subprocess.run(
        [
            "kubectl", "get", "deployment", cfg["target"], "-n", cfg["namespace"],
            "-o", "jsonpath={.spec.template.spec.containers[0].resources.limits.cpu}",
        ],
        capture_output=True, text=True,
    )
    current_limit = result.stdout.strip()
    if current_limit == CPU_THROTTLE_BASELINE_CPU_LIMIT:
        return

    print(f"  {cfg['target']}'s CPU limit is {current_limit or '(unknown)'}, not the "
          f"{CPU_THROTTLE_BASELINE_CPU_LIMIT} baseline -- resetting before injecting "
          f"(a prior real fix likely raised it)...")
    patch_body = (
        '{"spec":{"template":{"spec":{"containers":[{"name":"' + cfg["container"] + '",'
        '"resources":{"limits":{"cpu":"' + CPU_THROTTLE_BASELINE_CPU_LIMIT + '"}}}]}}}}'
    )
    subprocess.run(
        [
            "kubectl", "patch", "deployment", cfg["target"], "-n", cfg["namespace"],
            "--type=strategic", "-p", patch_body,
        ],
        capture_output=True, text=True,
    )
    subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=180s",
        ],
        capture_output=True, text=True,
    )


def _ensure_catalogue_replica_baseline(cfg: dict):
    """Resets catalogue's replica count back to
    UNDER_PROVISIONED_BASELINE_REPLICAS before injecting, if it's
    currently anything else -- mirrors _ensure_oom_baseline/
    _ensure_cpu_throttle_baseline's pattern exactly, same reason: a
    real successful scale-for-load fix permanently raises the replica
    count and nothing else reverts it."""
    result = subprocess.run(
        [
            "kubectl", "get", "deployment", cfg["target"], "-n", cfg["namespace"],
            "-o", "jsonpath={.status.availableReplicas}",
        ],
        capture_output=True, text=True,
    )
    current = result.stdout.strip()
    if current == str(UNDER_PROVISIONED_BASELINE_REPLICAS):
        return

    print(f"  {cfg['target']}'s available replicas is {current or '(unknown)'}, not the "
          f"{UNDER_PROVISIONED_BASELINE_REPLICAS} baseline -- resetting before injecting "
          f"(a prior real fix likely raised it)...")
    subprocess.run(
        [
            "kubectl", "scale", "deployment", cfg["target"], "-n", cfg["namespace"],
            f"--replicas={UNDER_PROVISIONED_BASELINE_REPLICAS}",
        ],
        capture_output=True, text=True,
    )
    subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=90s",
        ],
        capture_output=True, text=True,
    )


def _inject_and_verify_under_provisioned(cfg: dict) -> str | None:
    """Fires a real k6 burst (20 VUs, 20s, matching the validated
    Phase C2 measurement) directly against catalogue and confirms via
    k6's own real p95 output that it lands above
    UNDER_PROVISIONED_MIN_P95_MS -- mechanism assertion via a real
    active probe, not Chaos Mesh, not Prometheus. No persistent chaos
    resource to hold/delete -- see the constants' docstring above for
    why (the fault is a standing config state, not a transient
    condition)."""
    _ensure_catalogue_replica_baseline(cfg)
    namespace = cfg["namespace"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: firing a real "
              f"{UNDER_PROVISIONED_VUS}-VU/{UNDER_PROVISIONED_DURATION_S}s burst against catalogue...")
        p95_ms = _catalogue_burst_p95_ms(
            namespace, UNDER_PROVISIONED_VUS, UNDER_PROVISIONED_DURATION_S, "inject"
        )
        if p95_ms is not None and p95_ms >= UNDER_PROVISIONED_MIN_P95_MS:
            return "k6-burst"
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: p95={p95_ms}ms, below {UNDER_PROVISIONED_MIN_P95_MS}ms threshold{suffix}")
    return None


def _patch_front_end_image(cfg: dict, image: str):
    patch_body = (
        '{"spec":{"template":{"spec":{"containers":[{"name":"' + cfg["container"] + '",'
        '"image":"' + image + '"}]}}}}'
    )
    subprocess.run(
        [
            "kubectl", "patch", "deployment", cfg["target"], "-n", cfg["namespace"],
            "--type=strategic", "-p", patch_body,
        ],
        capture_output=True, text=True,
    )


def _ensure_front_end_image_baseline(cfg: dict):
    """Resets front-end's image back to FRONT_END_IMAGE_BASELINE before
    injecting, if it's currently anything else -- guards against a
    prior interrupted/failed run leaving it patched. Note: unlike
    oom/cpu-throttling/under-provisioned-replicas, this class's own
    REAL fix (rollback_deployment) is self-correcting by definition --
    a real successful rollback already returns the image to baseline,
    since "rollback" means "revert to the last known-good revision."
    This check exists only as a safety net for interrupted runs, not
    because the fix leaves elevated state the way those other
    classes' fixes do."""
    result = subprocess.run(
        [
            "kubectl", "get", "deployment", cfg["target"], "-n", cfg["namespace"],
            "-o", "jsonpath={.spec.template.spec.containers[0].image}",
        ],
        capture_output=True, text=True,
    )
    current_image = result.stdout.strip()
    if current_image == FRONT_END_IMAGE_BASELINE:
        return
    print(f"  {cfg['target']}'s image is {current_image or '(unknown)'}, not the "
          f"{FRONT_END_IMAGE_BASELINE} baseline -- resetting before injecting "
          f"(a prior interrupted run likely left it patched)...")
    _patch_front_end_image(cfg, FRONT_END_IMAGE_BASELINE)
    subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=90s",
        ],
        capture_output=True, text=True,
    )


def _front_end_image_pull_failing(namespace: str) -> bool:
    """True if any front-end-labeled pod currently reports a waiting
    reason of ImagePullBackOff or ErrImagePull -- confirmed empirically
    that this only ever matches the genuinely-stuck NEW pod (same
    "old pod stays healthy and untouched" pattern as init-failure's
    PAYMENT_READINESS_PATH_FAULT)."""
    query = (
        f'kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"front-end.*", reason=~"ImagePullBackOff|ErrImagePull"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _verify_bad_rollout_effect(namespace: str) -> bool:
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _front_end_image_pull_failing(namespace):
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _inject_and_verify_bad_rollout(cfg: dict) -> bool:
    """Patches front-end's image to a nonexistent tag via a strategic-
    merge kubectl patch, same mechanism class as init-failure/oom (a
    pod-template change that triggers a real RollingUpdate). Unlike
    init-failure (report-only, self-reverts), this is an AUTO-FIX
    class -- ground truth is left broken for the agent's own real fix
    (rollback_deployment) to resolve later, no self-revert here."""
    _ensure_front_end_image_baseline(cfg)
    namespace = cfg["namespace"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: patching front-end's image to a "
              f"nonexistent tag...")
        _patch_front_end_image(cfg, FRONT_END_IMAGE_FAULT)
        verified = _verify_bad_rollout_effect(namespace)
        if verified:
            return True
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: ImagePullBackOff/ErrImagePull never appeared{suffix}")
        _patch_front_end_image(cfg, FRONT_END_IMAGE_BASELINE)
    return False


def _patch_payment_readiness_path(cfg: dict, path: str):
    patch_body = (
        '{"spec":{"template":{"spec":{"containers":[{"name":"' + cfg["container"] + '",'
        '"readinessProbe":{"httpGet":{"path":"' + path + '"}}}]}}}}'
    )
    subprocess.run(
        [
            "kubectl", "patch", "deployment", cfg["target"], "-n", cfg["namespace"],
            "--type=strategic", "-p", patch_body,
        ],
        capture_output=True, text=True,
    )


def _restore_init_failure(cfg: dict):
    """Reverts payment's readinessProbe back to the real baseline path
    and waits for `kubectl rollout status` to confirm real recovery --
    never just assumes the patch API call succeeding means the fix
    landed (same 'verify real completion, not just API acceptance'
    discipline as restore_from_disk_full). Confirmed empirically this
    DOES succeed quickly on revert (unlike the fault-injecting patch,
    which structurally can never succeed) -- the reverted template
    matches the still-present, still-healthy OLD ReplicaSet exactly, so
    Kubernetes just scales the broken one back to 0 rather than
    creating a new pod. The broken ReplicaSet itself is left scaled to
    0 afterward -- normal Kubernetes revision history, auto-pruned at
    revisionHistoryLimit (default 10), confirmed NOT to accumulate the
    same way the NetworkChaos iptables chains did -- no active purge
    needed, checked per the fault-injection cleanup discipline rather
    than assumed clean."""
    _patch_payment_readiness_path(cfg, PAYMENT_READINESS_PATH_BASELINE)
    result = subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=90s",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: rollout status did not confirm recovery cleanly: {result.stderr.strip()[:300]}")


def _ensure_init_failure_baseline(cfg: dict):
    """Resets payment's readinessProbe path back to baseline before
    injecting, if it's currently anything else -- mirrors
    _ensure_oom_baseline's pattern, guards against a prior failed/
    interrupted run leaving it patched."""
    result = subprocess.run(
        [
            "kubectl", "get", "deployment", cfg["target"], "-n", cfg["namespace"],
            "-o", "jsonpath={.spec.template.spec.containers[0].readinessProbe.httpGet.path}",
        ],
        capture_output=True, text=True,
    )
    current_path = result.stdout.strip()
    if current_path == PAYMENT_READINESS_PATH_BASELINE:
        return
    print(f"  {cfg['target']}'s readinessProbe path is '{current_path or '(unknown)'}', not the "
          f"baseline '{PAYMENT_READINESS_PATH_BASELINE}' -- resetting before injecting "
          f"(a prior run likely left it patched)...")
    _restore_init_failure(cfg)


def _payment_stuck_not_ready(namespace: str) -> bool:
    """True if any payment-labeled pod currently reports Ready=false --
    confirmed empirically (2026-07-24) that this only ever matches the
    genuinely-stuck NEW pod, never the OLD healthy one (which stays
    Ready=true and untouched the entire time, see
    PAYMENT_READINESS_PATH_FAULT's docstring). No separate restart-
    count gate needed: a pod in this state never restarts (it's stuck
    pending, not crash-looping), so a plain Ready=false check doesn't
    risk colliding with crash-loop's own signal."""
    query = (
        f'kube_pod_status_ready{{namespace="{namespace}", pod=~"payment.*", '
        f'condition="false"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _verify_init_failure_effect(namespace: str) -> bool:
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _payment_stuck_not_ready(namespace):
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _inject_and_verify_init_failure(cfg: dict) -> bool:
    """Patches payment's readinessProbe.httpGet.path to a nonexistent
    endpoint via a strategic-merge kubectl patch, leaving livenessProbe
    untouched. NOT Chaos Mesh -- a direct Deployment patch, same
    mechanism class as oom's patch_memory_limit (both are pod-template
    changes that trigger a real RollingUpdate)."""
    _ensure_init_failure_baseline(cfg)
    namespace = cfg["namespace"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        window_start = time.time()
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: patching readinessProbe to a broken path...")
        _patch_payment_readiness_path(cfg, PAYMENT_READINESS_PATH_FAULT)
        verified = _verify_init_failure_effect(namespace)
        if verified:
            remaining = cfg["duration_s"] - (time.time() - window_start)
            if remaining > 0:
                time.sleep(remaining)
            _restore_init_failure(cfg)
            return True
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: readiness never flipped to false{suffix}")
        _restore_init_failure(cfg)
    return False


def _scale_deployment(cfg: dict, replicas: int):
    subprocess.run(
        ["kubectl", "scale", "deployment", cfg["target"], "-n", cfg["namespace"], f"--replicas={replicas}"],
        capture_output=True, text=True,
    )


def _replicas_available(namespace: str, deployment: str) -> int | None:
    """kube_deployment_status_replicas_available -- confirmed to exist
    on this cluster before writing this function, not assumed (real
    query, 2026-07-24, baseline value 1). Returns None if no data point
    exists yet, matching every other verify helper's convention."""
    query = f'kube_deployment_status_replicas_available{{namespace="{namespace}", deployment="{deployment}"}}'
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    return int(float(result[0]["value"][1]))


def _wait_for_replicas_available(namespace: str, deployment: str, target: int, timeout_s: int) -> bool:
    """Polls until replicas_available genuinely reaches the target
    count -- never just trusts that `kubectl scale` being accepted
    means the real state changed (the same 'verify real completion,
    not just API acceptance' lesson restore_from_disk_full's saga
    already taught the hard way)."""
    elapsed = 0
    while elapsed <= timeout_s:
        current = _replicas_available(namespace, deployment)
        if current == target:
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _ensure_session_failure_baseline(cfg: dict):
    """Resets session-db back to 1 replica before injecting, if it's
    currently anything else -- mirrors _ensure_oom_baseline/
    _ensure_init_failure_baseline's pattern, guards against a prior
    failed/interrupted run leaving it scaled down."""
    current = _replicas_available(cfg["namespace"], cfg["target"])
    if current == 1:
        return
    print(f"  {cfg['target']}'s available replicas is {current}, not the baseline of 1 -- "
          f"resetting before injecting (a prior run likely left it scaled down)...")
    _scale_deployment(cfg, 1)
    _wait_for_replicas_available(cfg["namespace"], cfg["target"], 1, SESSION_FAILURE_SCALE_TIMEOUT_S)


def _inject_and_verify_session_cart_failure(cfg: dict) -> bool:
    """Scales session-db to 0 for the fault window, then back to 1 --
    NOT a process kill/restart (which would produce the exact same
    restart-increase/CrashLoopBackOff signature crash-loop already
    owns, defeating this class's whole point). Scaling to 0 terminates
    the pod gracefully rather than restarting it, so it never touches
    the restart-count signal at all -- a genuinely new signal type
    (target has ZERO available replicas) distinct from every other
    class's signature (target pod exists but is broken in some way).
    While at 0, the Service has no endpoints, so any real login/cart/
    checkout call genuinely fails (connection refused, not a hang) --
    matches this class's real "login/cart failure" story, grounded in
    the actual session-db RDB-persistence bug already found+fixed
    2026-07-21."""
    _ensure_session_failure_baseline(cfg)
    namespace = cfg["namespace"]
    target = cfg["target"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        window_start = time.time()
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: scaling {target} to 0...")
        _scale_deployment(cfg, 0)
        verified = _wait_for_replicas_available(namespace, target, 0, SESSION_FAILURE_SCALE_TIMEOUT_S)
        if verified:
            remaining = cfg["duration_s"] - (time.time() - window_start)
            if remaining > 0:
                time.sleep(remaining)
            print(f"  restoring {target} to 1 replica, waiting for real recovery...")
            _scale_deployment(cfg, 1)
            recovered = _wait_for_replicas_available(namespace, target, 1, SESSION_FAILURE_SCALE_TIMEOUT_S)
            if not recovered:
                print(f"  WARNING: {target} did not confirm recovery to 1 replica within "
                      f"{SESSION_FAILURE_SCALE_TIMEOUT_S}s -- may need a manual check.")
            return True
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: replicas never reached 0{suffix}")
        _scale_deployment(cfg, 1)
        _wait_for_replicas_available(namespace, target, 1, SESSION_FAILURE_SCALE_TIMEOUT_S)
    return False


def record_episode(
    conn: sqlite3.Connection, episode_id: str, fault_class: str, cfg: dict, chaos_name: str, t0: str
):
    conn.execute(
        "INSERT INTO episodes (episode_id, fault_class, target, namespace, t0, chaos_resource_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (episode_id, fault_class, cfg["target"], cfg["namespace"], t0, chaos_name),
    )
    conn.commit()


def _inject_and_verify_disk_full(cfg: dict) -> bool:
    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
        if baseline_pod_name is None:
            # No Running pod at all -- writing nothing would spin for the
            # full window and then report a misleading "no eviction
            # detected". This is an infra/state problem, not a failed
            # injection: almost always the deployment is scaled to 0 or
            # thrashing (e.g. a fix left it at replicas=0). Fail loudly
            # and immediately with the real cause instead of retrying.
            print(
                f"  ABORT: no Running pod for {cfg['target']} in {cfg['namespace']} "
                f"-- deployment may be scaled to 0 or unhealthy. Check "
                f"`kubectl get deployment {cfg['target']} -n {cfg['namespace']}` "
                f"and scale back to 1 if needed. NOT a diagnosis/verifier issue."
            )
            return False
        since_ts = time.time()
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: running exec-based disk fill for {cfg['duration_s']}s...")
        run_disk_full_injection(cfg, cfg["duration_s"], baseline_pod_name, since_ts)
        try:
            verified = _verify_disk_full_effect(cfg["target"], cfg["namespace"], since_ts, baseline_pod_name)
        finally:
            # Cleanup must run even if verification itself throws (e.g. a
            # Prometheus hiccup) -- otherwise leftover files silently
            # poison the next episode on this target, exactly the bug
            # this whole verify-before-record fix exists to prevent.
            _cleanup_disk_full_files(cfg["target"], cfg["namespace"], cfg["container"])
        if verified:
            return True
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no eviction/pod-churn detected{suffix}")
    return False


def _inject_and_verify_crash_loop(cfg: dict) -> bool:
    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_restarts = _restart_count(cfg["target"], cfg["namespace"])
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: running exec-based kill loop for {cfg['duration_s']}s...")
        run_crash_loop_injection(cfg, cfg["duration_s"])
        verified = _verify_crash_loop_effect(cfg["target"], cfg["namespace"], baseline_restarts)
        if verified:
            return True
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no restart detected{suffix}")
    return False


def _inject_and_verify_network_latency(cfg: dict) -> str | None:
    """Unlike the other Chaos Mesh class (oom), verification here does
    NOT reuse _verify_restart_effect -- a network delay never restarts
    anything. Verified via _probe_orders_latency_ms's direct, timed
    requests (see the ABANDONED note above LATENCY_PROBE_SAMPLES for
    why this isn't k6/Prometheus-metric-based)."""
    chaos_kind = "networkchaos"
    namespace = cfg["namespace"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_ms = _probe_orders_latency_ms(namespace)
        if baseline_ms is None:
            print("  latency probe failed to run (image pull / scheduling issue?) -- treating baseline as 0")
            baseline_ms = 0.0

        chaos_name = f"{cfg['chaos_name_prefix']}-{uuid.uuid4().hex[:8]}"
        manifest = build_network_latency_manifest(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: baseline={baseline_ms}ms, "
              f"holding the fault active for the full {cfg['duration_s']}s window...")

        # Found the hard way (2026-07-21): originally broke out of this
        # loop (and deleted the chaos resource) the moment OUR OWN
        # probe confirmed the effect -- often within ~10-20s. That cut
        # the real fault short well before its intended duration_s,
        # starving traffic_gen's own organic (infrequent, ~1
        # request/2-3s) k6 traffic of any real chance to also observe
        # it -- confirmed via a Prometheus range query showing k6's
        # elevated latency sample for one episode didn't even land
        # until 70-105s after the fault started, by which point the
        # agent had already been asked to diagnose (injector-end + 35s
        # settle) and found nothing. The probe still runs throughout
        # to determine pass/fail, but no longer ends the fault early --
        # it now runs its full duration_s like every other class
        # (crash-loop/oom/disk-full's own injection loops already do
        # this naturally).
        verified = False
        elapsed = 0
        try:
            while elapsed < cfg["duration_s"]:
                time.sleep(10)
                elapsed += 10
                during_ms = _probe_orders_latency_ms(namespace)
                if during_ms is not None and during_ms >= baseline_ms + NETWORK_LATENCY_MIN_INCREASE_MS:
                    verified = True
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)

        if verified:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no latency increase observed (baseline={baseline_ms}ms){suffix}")
    return None


def _inject_and_verify_network_partition(cfg: dict) -> str | None:
    """Verified via _probe_orders_reachable (direct probe, NOT k6/
    Prometheus -- see NETWORK_PARTITION_PROBE_SAMPLES's docstring for
    why k6_http_req_failed is unusable here: front-end's own call to
    orders has no timeout and hangs indefinitely rather than ever
    failing observably while the partition holds).

    Probes once early (after a short propagation wait) to confirm the
    block landed for real, then holds the fault for its FULL
    duration_s -- same "don't end early" discipline as network-latency,
    disk-full, memory-leak, and connection-pool-exhaustion all
    independently learned the hard way: ending a fault the instant our
    own probe is satisfied starves any other real observer (traffic_gen,
    a future real diagnosis query) of a fair chance to see it too.
    Deliberately does NOT probe again near the end of the window --
    confirmed during measurement (2026-07-24) that a probe landing right
    at the CR's natural expiry boundary can show a false partial
    recovery purely from probe-pod scheduling overhead, not a real
    leaky block."""
    chaos_kind = "networkchaos"
    namespace = cfg["namespace"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        chaos_name = f"{cfg['chaos_name_prefix']}-{uuid.uuid4().hex[:8]}"
        manifest = build_network_partition_manifest(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: applied, waiting 5s for propagation "
              f"before probing...")

        verified = False
        window_start = time.time()
        try:
            time.sleep(5)
            failures = _probe_orders_reachable(namespace)
            verified = failures >= NETWORK_PARTITION_MIN_FAILURES
            print(f"  early probe: {failures}/{NETWORK_PARTITION_PROBE_SAMPLES} samples failed "
                  f"(need >= {NETWORK_PARTITION_MIN_FAILURES})")

            # Sleep out whatever's genuinely left of duration_s, based on
            # REAL elapsed wall-clock time (the 5s wait + the probe's own
            # real scheduling/curl overhead), not an assumed constant --
            # the exact lesson this class's own measurement scripts
            # taught about probe overhead eating into fault windows.
            remaining = cfg["duration_s"] - (time.time() - window_start)
            if remaining > 0:
                time.sleep(remaining)
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)

        if verified:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: partition did not verify{suffix}")
    return None


def _inject_and_verify_memory_leak(cfg: dict) -> str | None:
    """Verified via cAdvisor's own container_memory_working_set_bytes
    (a reliable, real-time kube-native metric -- not the k6/Prometheus
    percentile that turned out unreliable for network-latency). TWO
    conditions must hold: memory rose by a real margin over baseline,
    AND the pod never restarted -- a restart would mean the stressor
    was sized too large and this was actually an OOM, not a leak (the
    sibling, but structurally different, fault class). Holds for the
    FULL duration_s every attempt, same fix applied to network-latency
    after learning the hard way that ending a fault early starves any
    real external observer (here: the agent's own later diagnosis
    query) of a fair chance to see it."""
    chaos_kind = "stresschaos"
    namespace = cfg["namespace"]
    target = cfg["target"]
    container = cfg["container"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_mib = _memory_working_set_mib(target, namespace, container)
        baseline_restarts = _restart_count(target, namespace)
        if baseline_mib is None:
            print("  no container_memory_working_set_bytes data yet -- treating baseline as 0")
            baseline_mib = 0.0

        chaos_name = f"{cfg['chaos_name_prefix']}-{uuid.uuid4().hex[:8]}"
        manifest = build_memory_leak_manifest(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: baseline={baseline_mib}MiB, "
              f"holding the fault active for the full {cfg['duration_s']}s window...")

        restarted = False
        peak_mib = baseline_mib
        elapsed = 0
        try:
            while elapsed < cfg["duration_s"]:
                time.sleep(10)
                elapsed += 10
                current_mib = _memory_working_set_mib(target, namespace, container)
                if current_mib is not None:
                    peak_mib = max(peak_mib, current_mib)
                if _restart_count(target, namespace) > baseline_restarts:
                    restarted = True
                    break  # stop early -- this is an OOM now, not a leak, no point holding further
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)

        if restarted:
            print(f"  attempt {attempt}: pod restarted during injection (stressor too large -- "
                  f"this was an OOM, not a leak){', retrying' if attempt < MAX_INJECT_ATTEMPTS else ''}")
            continue

        if peak_mib >= baseline_mib + MEMORY_LEAK_MIN_INCREASE_MIB:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no sustained memory increase observed "
              f"(baseline={baseline_mib}MiB, peak={peak_mib}MiB){suffix}")
    return None


def _flood_connections(cfg: dict) -> bool:
    """Single kubectl exec into catalogue-db backgrounds
    CONNECTION_POOL_FLOOD_CONNECTIONS real mysql client processes (each
    holding a genuine connection open via SELECT SLEEP), then returns
    immediately once the loop finishes issuing them -- the backgrounded
    children keep running inside the container after this exec session
    ends (no Chaos Mesh involved at all; there's no primitive for this,
    so this is a direct real mechanism like crash-loop/disk-full).
    Real capacity consumed against MySQL's own max_connections (151,
    confirmed empirically), not simulated."""
    namespace = cfg["namespace"]
    container = cfg["container"]
    target = cfg["target"]
    pod_name = _current_pod_name(target, namespace)
    if pod_name is None:
        return False
    sleep_s = cfg["duration_s"] + 15  # outlives our own polling window with margin
    script = (
        f"for i in $(seq 1 {CONNECTION_POOL_FLOOD_CONNECTIONS}); do "
        f'mysql -u{CONNECTION_POOL_FLOOD_USER} -p{CONNECTION_POOL_FLOOD_PASSWORD} '
        f'-e "SELECT SLEEP({sleep_s})" '
        f">/dev/null 2>&1 & done"
    )
    result = subprocess.run(
        ["kubectl", "exec", "-n", namespace, pod_name, "-c", container, "--", "sh", "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def _test_connection_fails(cfg: dict) -> bool:
    """Attempts ONE additional real connection -- using catalogue's OWN
    actual DSN credentials (catalogue_user, not root -- see
    CONNECTION_POOL_TEST_USER docstring for why root would falsely
    succeed via MySQL's reserved-slot mechanism even during real
    exhaustion). Returns True only if it fails with MySQL's own real
    'too many connections' error, confirming genuine exhaustion rather
    than inferring it from a threshold."""
    namespace = cfg["namespace"]
    container = cfg["container"]
    pod_name = _current_pod_name(cfg["target"], namespace)
    if pod_name is None:
        return False
    result = subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "mysql", f"-u{CONNECTION_POOL_TEST_USER}", f"-p{CONNECTION_POOL_TEST_PASSWORD}",
            "-e", "SELECT 1",
        ],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode != 0 and "Too many connections" in (result.stderr or "")


def _cleanup_connection_flood(cfg: dict):
    namespace = cfg["namespace"]
    container = cfg["container"]
    pod_name = _current_pod_name(cfg["target"], namespace)
    if pod_name is None:
        return
    subprocess.run(
        ["kubectl", "exec", "-n", namespace, pod_name, "-c", container, "--", "pkill", "-f", "SELECT SLEEP"],
        capture_output=True, text=True,
    )


def _inject_and_verify_connection_pool_exhaustion(cfg: dict) -> str | None:
    """Verified by actually attempting one more real connection and
    confirming it fails with MySQL's genuine 'too many connections'
    error -- the same thing catalogue itself would experience, not an
    inferred threshold. Holds for the full duration_s every attempt,
    same fix applied to network-latency/memory-leak after learning the
    hard way that ending a fault early starves the agent's later
    diagnosis query of a fair chance to see it."""
    duration_s = cfg["duration_s"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: flooding "
              f"{CONNECTION_POOL_FLOOD_CONNECTIONS} connections, holding for the full {duration_s}s window...")
        flooded = _flood_connections(cfg)
        if not flooded:
            print(f"  attempt {attempt}: failed to launch the connection flood "
                  f"(pod not found / exec error){', retrying' if attempt < MAX_INJECT_ATTEMPTS else ''}")
            continue

        try:
            time.sleep(5)  # give the flood a moment to actually establish all connections
            verified = _test_connection_fails(cfg)
            remaining = duration_s - 5
            if remaining > 0:
                time.sleep(remaining)
        finally:
            _cleanup_connection_flood(cfg)

        if verified:
            return "manual-exec"
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: additional test connection did NOT fail -- "
              f"flood may not have reached real capacity{suffix}")
    return None


def _verify_cpu_throttle_effect(
    target: str, namespace: str, container: str, baseline_periods: int
) -> bool:
    """Polls for a real delta above CPU_THROTTLE_MIN_PERIODS_INCREASE,
    same scrape-lag-tolerant pattern as _verify_restart_effect."""
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        current = _cfs_throttled_periods(target, namespace, container)
        if current - baseline_periods >= CPU_THROTTLE_MIN_PERIODS_INCREASE:
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _inject_and_verify_cpu_throttling(cfg: dict) -> str | None:
    """StressChaos cpu stressor against `user`, held for the full
    duration_s (same 'don't end early' discipline every other class
    learned the hard way -- an external observer, or a future real
    diagnosis call, needs a fair chance to see the fault too). Verified
    via a raw before/after delta on container_cpu_cfs_throttled_periods_total,
    not Chaos Mesh's own state."""
    _ensure_cpu_throttle_baseline(cfg)
    chaos_kind = "stresschaos"
    namespace = cfg["namespace"]
    target = cfg["target"]
    container = cfg["container"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_periods = _cfs_throttled_periods(target, namespace, container)
        chaos_name = f"{cfg['chaos_name_prefix']}-{uuid.uuid4().hex[:8]}"
        manifest = build_cpu_throttle_manifest(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: baseline_periods={baseline_periods}, "
              f"holding the fault active for the full {cfg['duration_s']}s window...")
        time.sleep(cfg["duration_s"])
        try:
            verified = _verify_cpu_throttle_effect(target, namespace, container, baseline_periods)
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)
        if verified:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no real throttled-periods increase observed{suffix}")
    return None


def _inject_and_verify_chaos_mesh(fault_class: str, cfg: dict, manifest_builder) -> str | None:
    """Returns the chaos_name of the attempt that got verified, or None
    if all attempts failed. Only oom uses this now -- crash-loop moved
    to the exec-based mechanism above, disk-full never used Chaos Mesh
    at all."""
    chaos_kind = "stresschaos"
    baseline_restarts = _restart_count(cfg["target"], cfg["namespace"])

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        chaos_name = f"{cfg.get('chaos_name_prefix', fault_class)}-{uuid.uuid4().hex[:8]}"
        manifest = manifest_builder(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: letting it run for {cfg['duration_s']}s...")
        time.sleep(cfg["duration_s"])
        try:
            verified = _verify_restart_effect(cfg["target"], cfg["namespace"], baseline_restarts)
        finally:
            # Cleanup must run even if verification throws -- an active
            # Schedule left behind on a connection hiccup would keep
            # killing the target pod indefinitely, corrupting every
            # future episode on it until someone notices manually.
            delete_chaos_resource(chaos_kind, chaos_name)
        if verified:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no restart detected{suffix}")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--class", dest="fault_class", required=True, choices=FAULT_CONFIG.keys())
    args = parser.parse_args()

    fault_class = args.fault_class
    cfg = FAULT_CONFIG[fault_class]

    episode_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc).isoformat()
    print(f"Episode {episode_id}: attempting {fault_class} on {cfg['target']} ({cfg['namespace']}) at {t0}")

    if fault_class == "disk-full":
        verified = _inject_and_verify_disk_full(cfg)
        chaos_name = "manual-exec" if verified else None
    elif fault_class == "crash-loop":
        verified = _inject_and_verify_crash_loop(cfg)
        chaos_name = "manual-exec" if verified else None
    elif fault_class == "network-latency":
        chaos_name = _inject_and_verify_network_latency(cfg)
    elif fault_class == "memory-leak":
        chaos_name = _inject_and_verify_memory_leak(cfg)
    elif fault_class == "connection-pool-exhaustion":
        chaos_name = _inject_and_verify_connection_pool_exhaustion(cfg)
    elif fault_class == "network-partition":
        chaos_name = _inject_and_verify_network_partition(cfg)
    elif fault_class == "init-failure":
        verified = _inject_and_verify_init_failure(cfg)
        chaos_name = "manual-patch" if verified else None
    elif fault_class == "session-cart-failure":
        verified = _inject_and_verify_session_cart_failure(cfg)
        chaos_name = "manual-scale" if verified else None
    elif fault_class == "cpu-throttling":
        chaos_name = _inject_and_verify_cpu_throttling(cfg)
    elif fault_class == "under-provisioned-replicas":
        chaos_name = _inject_and_verify_under_provisioned(cfg)
    elif fault_class == "bad-rollout":
        verified = _inject_and_verify_bad_rollout(cfg)
        chaos_name = "manual-patch" if verified else None
    elif fault_class == "oom":
        _ensure_oom_baseline(cfg)
        chaos_name = _inject_and_verify_chaos_mesh(fault_class, cfg, build_oom_manifest)
    else:
        chaos_name = _inject_and_verify_chaos_mesh(fault_class, cfg, build_oom_manifest)

    if not chaos_name:
        print(
            f"INJECTION FAILED after {MAX_INJECT_ATTEMPTS} attempts for {fault_class} on "
            f"{cfg['target']} -- NO episode recorded. If this keeps happening, the cluster "
            f"(or Chaos Mesh's own daemon) is unhealthy, not the diagnosis/verifier code."
        )
        return

    conn = ensure_db()
    record_episode(conn, episode_id, fault_class, cfg, chaos_name, t0)
    conn.close()
    print(f"Episode {episode_id}: injection verified ({chaos_name}) and ground truth recorded.")


if __name__ == "__main__":
    main()
