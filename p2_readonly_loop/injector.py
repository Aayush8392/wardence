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
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from agent import OOM_STICKY_MAX_CONTAINER_AGE_S

PROMETHEUS_URL = "http://localhost:9090"
MAX_INJECT_ATTEMPTS = 3
EFFECT_VERIFY_TIMEOUT_S = 35  # covers kube-state-metrics' ~30s scrape cycle
EFFECT_VERIFY_POLL_S = 5

# oom's own verification, 2026-08-01 (Kimi review 19) -- polls the k8s
# API directly instead of a Prometheus counter, so no scrape-lag
# margin is needed here the way EFFECT_VERIFY_TIMEOUT_S has to budget
# for. OOM_VERIFY_CEILING_S is a real backstop against a stressor that
# never wins, not a guess at "how long a kill takes" -- see
# _inject_and_verify_oom's docstring for the real 97s/119s data this
# was sized against.
OOM_VERIFY_CEILING_S = 200
OOM_VERIFY_POLL_S = 3

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
        # Widened 60s -> 90s, 2026-08-01: real root cause found for 3
        # consecutive real injection failures in the first LLM overnight
        # batch -- a real container that DID get OOMKilled took 97s
        # start-to-kill (confirmed via kubectl's lastState.terminated,
        # startedAt=00:26:30/finishedAt=00:28:07), but the old 60s hold
        # + 35s verify-poll budget (EFFECT_VERIFY_TIMEOUT_S) only gave
        # 95s total -- a razor-thin, effectively negative margin. 90s
        # gives a 125s total budget, real margin over the observed 97s.
        "duration_s": 90,
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
#
# Raised 1 -> 6 workers, 2026-08-1x: 1 worker reliably tripped the real
# detection threshold below (confirmed via cfs_throttled_periods delta)
# but produced no human-perceptible request-latency change on live
# storefront testing -- a real, measured finding, not assumed. Root
# cause reasoned through: login/account requests are mostly I/O-bound
# (waiting on MongoDB), so even with the cgroup's CFS bandwidth quota
# fully saturated by 1 worker, a request's own tiny CPU burst could
# still slip into the next ~100ms period with little visible delay.
# More workers doesn't increase the bandwidth throttling further (quota
# was already saturated at 1) -- it increases real OS-level scheduling
# CONTENTION, i.e. the app thread has more competing runnable workers
# to wait behind even once it IS scheduled. Still fully contained
# within user's own 300m cgroup ceiling (the quota is a hard cap on
# total CPU-time for everything in that cgroup combined, workers
# included) -- no node-wide blast-radius change, confirmed by the
# mechanism itself, not assumed. 6 chosen against the real machine's
# 8-core/12-thread spec (Local Infra Strategy, wardence_context.md) --
# substantial real contention, not an attempt to starve the whole node.
# NOTE: this changes what "cpu-throttling" actually IS as an injected
# fault -- episodes recorded before this change are a different real
# fault intensity than episodes after it, same methodology-continuity
# note this project already logged for connection-pool-exhaustion's
# flood-size retuning and disk-full's ephemeral-limit retuning.
CPU_THROTTLE_STRESS_WORKERS = 6
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
UNDER_PROVISIONED_MIN_P95_MS = 190
UNDER_PROVISIONED_BASELINE_REPLICAS = 1
# Real bug found and fixed 2026-08-06: check_safe()'s scale_deployment
# bound used to only require proposed > current_replicas (1) -- "safe"
# by that bar alone, but never checked against the ONE real measured
# data point above (3 replicas -> p95 100-291ms, safely below the
# 190/200ms threshold). A real live episode proposed 2 replicas,
# passed the old bound, dispatched for real, and genuinely failed
# durability (p95 stayed >=200ms) -- 2 was never empirically tested,
# only 1 and 3 were. Named here so constraint_checks.py can floor its
# safety bound at this real, measured-safe value, not just "better
# than broken."
UNDER_PROVISIONED_VALIDATED_SAFE_REPLICAS = 3

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
                "-n", namespace, f"--image={K6_IMAGE}", "--image-pull-policy=IfNotPresent",
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
#
# Found a THIRD time (2026-07-29/30, real overnight batch run): the
# exact same drift pattern recurred -- 5 days after the last manual
# re-measurement, the flood needed 2-3 retries on multiple chunks and
# failed all 3 attempts outright on one, triggering run_batch_plan.py's
# real give-up-on-this-class safety mechanism. A static, hand-tuned
# number will ALWAYS eventually go stale again as real baseline
# Threads_connected keeps growing with accumulated testing -- the real
# fix is measuring it live every attempt instead of re-guessing a
# fourth static value. See _compute_flood_target() below. This constant
# now only serves as the historical-floor fallback if a live baseline
# read ever fails.
CONNECTION_POOL_FLOOD_CONNECTIONS = 170
CATALOGUE_DB_MAX_CONNECTIONS = 151
# Real, measured rate at which targeted flood connections actually
# establish (144/150 landed in the 2026-07-25 diagnostic, a real MySQL
# boundary-condition effect, not a bug) -- baked into the dynamic
# flood-size formula as a safety factor, not just padded and hoped.
CONNECTION_POOL_ESTABLISH_SUCCESS_RATE = 0.95
# Real margin beyond just barely filling the pool, so a test connection
# genuinely has nothing left rather than winning a coin-flip against the
# last open slot.
CONNECTION_POOL_SAFETY_MARGIN = 15

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
# Found the hard way (2026-07-27, while debugging network-partition):
# every kubectl-run call using this image (here and K6_IMAGE below) was
# missing an explicit --image-pull-policy, so Kubernetes' own default
# rule for an untagged/":latest" image (imagePullPolicy: Always) forced
# a real Docker Hub registry round-trip on EVERY single probe
# invocation, even though the image was already cached locally --
# violating this project's own self-containment principle (the only
# real external dependency should ever be the LLM call) and adding
# real, unpredictable latency to every probe. Confirmed as a real,
# separate failure mode, not just theoretical: a probe pod outright
# failed with ErrImagePull ("TLS handshake timeout" reaching
# auth.docker.io) mid-investigation, even though `crictl images`
# already showed curlimages/curl cached. Fixed by adding
# --image-pull-policy=IfNotPresent to every kubectl-run call using
# these images, forcing local-cache reuse and removing the live
# registry dependency entirely.
# Bumped from 30 (2026-07-27): a clean, idle `kubectl run --rm` round trip
# measured 28.03s real -- almost entirely pod scheduling/API overhead, not
# the curl requests. Left only ~2s of headroom even when nothing else was
# happening; a cluster under real load (a long batch of back-to-back
# episodes) reliably tipped this over into an uncaught timeout crash. See
# _probe_orders_latency_ms's except clause for the other half of this fix.
LATENCY_PROBE_TIMEOUT_S = 50

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


# Real cross-process lock, closes Kimi review 34 findings #2/#6, 2026-08-1x.
# run_batch_plan.py and operator_api.py's live-trigger path both ultimately
# shell out to THIS script as a subprocess, but neither can see the other
# is running -- operator_api.py's own _TRIGGER_BUSY is an in-memory flag
# scoped to its own process, structurally invisible to a separate
# run_batch_plan.py process. This script is the one real entry point every
# trigger path shares (same reasoning _clear_stale_oom_sticky_flag's own
# placement in main() already uses), so it's the right place to make "only
# one real cluster mutation in flight, system-wide" a real cross-process
# guarantee instead of one that only holds within a single process.
#
# 600s staleness margin -- comfortably past oom's real worst-case ~500s
# subprocess cost (the longest of any class) -- so a crashed process that
# never released the lock can't wedge the system forever; a stale lock is
# treated as free, same bounded-staleness reasoning as operator_api.py's
# own EPISODE_IN_FLIGHT_MAX_AGE_MINUTES.
SYSTEM_LOCK_STALE_S = 600
SYSTEM_LOCK_RETRY_S = 5
# Fail fast and loud rather than hang silently -- a genuine collision
# should be rare and is worth surfacing immediately, not papered over
# with a long wait that just delays the same eventual failure.
SYSTEM_LOCK_MAX_WAIT_S = 30


def _ensure_system_lock_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_lock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            holder TEXT,
            acquired_at REAL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO system_lock (id, holder, acquired_at) VALUES (1, NULL, NULL)"
    )
    conn.commit()


def _acquire_system_lock(conn, holder: str) -> bool:
    """Atomic acquire: succeeds if the row is free OR its holder is stale.
    Real rowcount check under sqlite3's own implicit transaction -- same
    check-and-set-atomically pattern operator_api.py's own
    _try_acquire_trigger_busy already uses, just DB-backed instead of an
    in-memory dict so it's visible across processes."""
    now = time.time()
    cur = conn.execute(
        """
        UPDATE system_lock SET holder = ?, acquired_at = ?
        WHERE id = 1 AND (holder IS NULL OR acquired_at < ?)
        """,
        (holder, now, now - SYSTEM_LOCK_STALE_S),
    )
    conn.commit()
    return cur.rowcount == 1


def _release_system_lock(conn, holder: str):
    """Only releases a lock this exact holder actually acquired -- a run
    that lost the race (or a stale lock it never held) can never release
    someone else's real, still-active lock."""
    conn.execute(
        "UPDATE system_lock SET holder = NULL, acquired_at = NULL WHERE id = 1 AND holder = ?",
        (holder,),
    )
    conn.commit()


def acquire_system_lock_or_die(holder: str):
    conn = ensure_db()
    _ensure_system_lock_table(conn)
    waited = 0
    while not _acquire_system_lock(conn, holder):
        if waited >= SYSTEM_LOCK_MAX_WAIT_S:
            conn.close()
            raise SystemExit(
                f"Could not acquire the cross-process system lock after "
                f"{SYSTEM_LOCK_MAX_WAIT_S}s -- another injector.py run (batch or "
                f"live-trigger) appears to genuinely be in flight. Refusing to "
                f"start a second concurrent cluster mutation."
            )
        print(f"  system lock held by another run -- waiting ({waited}s/{SYSTEM_LOCK_MAX_WAIT_S}s)...")
        time.sleep(SYSTEM_LOCK_RETRY_S)
        waited += SYSTEM_LOCK_RETRY_S
    conn.close()


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
                "-n", namespace, f"--image={LATENCY_PROBE_IMAGE}", "--image-pull-policy=IfNotPresent",
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
                "-n", namespace, f"--image={LATENCY_PROBE_IMAGE}", "--image-pull-policy=IfNotPresent",
                "--", "sh", "-c", script,
            ],
            capture_output=True, text=True, timeout=LATENCY_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Found the hard way (2026-07-27): a clean, idle `kubectl run --rm`
        # round trip already measured 28.03s -- almost entirely pod
        # scheduling/API overhead, not the curl requests themselves (those
        # took ~1.5s combined). Only ~2s of headroom under
        # LATENCY_PROBE_TIMEOUT_S even when nothing else is going on, so a
        # cluster under real load (e.g. mid-way through a long batch of
        # back-to-back episodes) tips over it reliably. This used to crash
        # the whole injector with an uncaught traceback -- both callers of
        # this function already correctly treat a None return as "can't
        # verify" (never as "zero latency"), exactly matching this
        # function's own docstring, which promised this behavior without
        # actually catching the timeout that triggers it. Return None here
        # so a slow-but-not-broken cluster degrades into a retry, not a crash.
        return None
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


def run_crash_loop_injection(cfg: dict, duration_s: int, stop_file: str | None = None) -> bool:
    """
    Repeatedly SIGKILLs the container's real application process (by
    name pattern, NOT PID 1 -- see _kill_main_process) via kubectl exec
    on the SAME pod (in-place restart, same as the old container-kill
    action), so restarts genuinely accumulate and kubelet can back off
    into CrashLoopBackOff. Not a Chaos Mesh resource -- see module
    docstring for why.

    stop_file (Operator's early-exit mechanism, Kimi review 36 finding
    2/7 -- a file path, not a DB poll or signal, since this function has
    no SQLite connection to Operator's DB and shouldn't gain one just
    for this): checked once per tick (~CRASH_LOOP_KILL_INTERVAL_S). If
    it appears, the loop stops at its next natural checkpoint -- never
    mid-kubectl-exec -- and this returns True (interrupted early) rather
    than False (ran the full duration_s), so the caller knows not to
    retry a user-requested early stop. omit for batch runs; the check is
    then skipped entirely, identical to today's behavior.
    """
    end_time = time.time() + duration_s
    while time.time() < end_time:
        if stop_file is not None and os.path.exists(stop_file):
            return True
        pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
        if pod_name is None:
            time.sleep(2)
            continue
        _kill_main_process(pod_name, cfg["namespace"], cfg["container"], cfg["kill_pattern"])
        time.sleep(CRASH_LOOP_KILL_INTERVAL_S)
    return False


def _restart_count(target: str, namespace: str) -> int:
    """Prefix-match sum was originally reasoned as fine here (called only
    as a pre-injection baseline, before anything has happened to create
    ambiguity between an old and new pod) -- but that reasoning only
    covered old-vs-new-pod timing, not cross-SERVICE collisions.
    CORRECTED 2026-07-31 (same real bug as agent.py's own fix, see its
    docstring): several targets have a same-namespace <target>-db sibling
    service (carts/carts-db, orders/orders-db, user/user-db,
    catalogue/catalogue-db) that a bare "{target}.*" prefix match also
    catches -- an unrelated restart on the sibling service would
    corrupt this baseline (or a later verification comparison against
    it) with a completely unrelated event. Anchored to the real k8s
    pod-name shape (exactly two more hyphen-separated segments after the
    target name) to exclude it."""
    query = f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod=~"{target}-[^-]+-[^-]+$"}}'
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
        f'pod=~"{target}-[^-]+-[^-]+$", container="{container}"}}'
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
        f'pod=~"{target}-[^-]+-[^-]+$", container="{container}"}}'
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
        f'pod=~"{target}-[^-]+-[^-]+$", reason="CrashLoopBackOff"}} == 1'
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
        f'pod=~"{target}-[^-]+-[^-]+$", reason="Evicted"}} == 1) '
        f'and on(namespace, pod) (kube_pod_deletion_timestamp'
        f'{{namespace="{namespace}", pod=~"{target}-[^-]+-[^-]+$"}} > {since_ts})'
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
    is the common case (only matters right after a real fix cycle).
    """
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
    # non-baseline limit) while it's mid-termination. 300s not the old
    # 180s -- real live bug found 2026-08-01: the app's own readiness
    # probe has a 180s initial delay (see catalogue's real deployment
    # spec), so a 180s rollout-status timeout races that exact number
    # and can time out on a genuinely healthy rollout, not a stuck one.
    subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=300s",
        ],
        capture_output=True, text=True,
    )


def _clear_stale_oom_sticky_flag(cfg: dict):
    """Real, live-verified fix (2026-08-01) -- real incident: a real
    under-provisioned-replicas episode fired on catalogue only ~4
    minutes after a real oom episode got misdiagnosed as oom again,
    because agent.py's sticky-OOM signal (bound to
    container_start_time_seconds, deliberately kept true for
    OOM_STICKY_MAX_CONTAINER_AGE_S=1200s so a delayed diagnosis still
    catches a real kill) was still genuinely true. run_batch_plan.py's
    own recency-wait guards against this in the batch runner, but a
    real user firing faults from the live Operator frontend has no
    schedule to protect it -- called from main() below (not from
    run_batch_plan.py-only _ensure_oom_baseline) specifically because
    main() is the one real entry point EVERY trigger path shares.

    Queries the EXACT same sticky-OOM PromQL agent.py's own
    oom_sticky_query uses, and if it's genuinely still active, forces a
    real rollout restart to clear it (a fresh pod has no termination
    history to match "OOMKilled" against at all) rather than passively
    waiting out the full 1200s. Live-verified 2026-08-01: triggered a
    real oom episode, confirmed the sticky query matched, ran a real
    rollout restart, confirmed the same query returned empty afterward
    -- not just theorized. Cheap no-op in the common case (query empty),
    so safe to call unconditionally on every real episode, not just
    ones that happen to be oom."""
    query = (
        f'kube_pod_container_status_last_terminated_reason{{namespace="{cfg["namespace"]}", '
        f'pod=~"{cfg["target"]}-[^-]+-[^-]+$", reason="OOMKilled"}} == 1 '
        f'and on(namespace, pod, container) ((time() - container_start_time_seconds'
        f'{{namespace="{cfg["namespace"]}", pod=~"{cfg["target"]}-[^-]+-[^-]+$"}}) '
        f'< {OOM_STICKY_MAX_CONTAINER_AGE_S})'
    )
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
    except requests.RequestException:
        return  # real Prometheus hiccup -- never block/crash the batch over a visibility check

    if not result:
        return

    print(f"  {cfg['target']}'s sticky-OOM diagnostic signal is still active from a prior real "
          f"kill -- restarting to clear it before any other class diagnoses this target...")
    subprocess.run(
        ["kubectl", "rollout", "restart", f"deployment/{cfg['target']}", "-n", cfg["namespace"]],
        capture_output=True, text=True,
    )
    subprocess.run(
        [
            "kubectl", "rollout", "status", f"deployment/{cfg['target']}", "-n", cfg["namespace"],
            "--timeout=300s",
        ],
        capture_output=True, text=True,
    )


def _ensure_queue_master_pod_cleanup(cfg: dict):
    """Deletes any leftover Failed/Evicted queue-master pod object from a
    PRIOR disk-full episode, before the next injection starts -- mirrors
    _ensure_oom_baseline's "reset right before injecting, not immediately
    after fixing" pattern, moved here specifically because the scorer's
    own /diagnose call (which runs between one episode's injection and
    the next) needs the evicted pod to still exist to correctly diagnose
    disk-full at all (see _inject_and_verify_disk_full's own docstring
    for the real regression this replaced).

    Computed as ALL pods for this label minus the currently-Running one
    (via the existing _current_pod_name helper, --field-selector-based,
    already proven reliable) rather than a kubectl jsonpath filter
    expression (?(@.status.phase!=...)) -- kubectl's jsonpath filter
    support is a known-unreliable subset, not worth trusting for
    something a plain field-selector already does robustly elsewhere in
    this file."""
    all_result = subprocess.run(
        [
            "kubectl", "get", "pods", "-n", cfg["namespace"],
            "-l", f"name={cfg['target']}",
            "-o", "jsonpath={.items[*].metadata.name}",
        ],
        capture_output=True, text=True,
    )
    all_pods = all_result.stdout.split()
    running_pod = _current_pod_name(cfg["target"], cfg["namespace"])
    dead_pods = [p for p in all_pods if p != running_pod]
    for pod_name in dead_pods:
        print(f"  cleaning up leftover non-Running {cfg['target']} pod {pod_name} from a prior episode...")
        subprocess.run(
            ["kubectl", "delete", "pod", pod_name, "-n", cfg["namespace"], "--ignore-not-found"],
            capture_output=True, text=True,
        )


def _ensure_cpu_throttle_baseline(cfg: dict):
    """Resets user's CPU limit back to CPU_THROTTLE_BASELINE_CPU_LIMIT
    before injecting, if it's currently anything else -- mirrors
    _ensure_oom_baseline's pattern exactly, same reason: a real
    successful patch_cpu_limit fix permanently raises the limit and
    nothing else reverts it.

    Reworked 2026-07-28 to use the pods/resize subresource (KEP-1287,
    in-place pod vertical scaling) instead of patching the Deployment
    template -- confirmed manually first (not assumed): this cluster is
    k3s v1.36 and genuinely supports it (verified via a real test on the
    live `user` pod -- cpu limit changed 300m->400m->300m with restart
    count staying at 0 throughout, no rollout at all). A Deployment
    patch forces a full pod replace-and-wait every single time this
    class has actually earned trust and been fixed for real, which was
    the single biggest cost in a full-class timing batch (~280s of
    cpu-throttling's ~380s total). The resize API requires the COMPLETE
    current resources block in the merge patch (confirmed the hard way:
    a partial patch specifying only the changed field gets rejected --
    "resource limits/requests cannot be removed" -- resize treats an
    omitted field as a removal, not a no-op), so this reads the pod's
    real current resources first rather than hardcoding them. Falls
    back to the original Deployment-patch-and-wait path if the resize
    attempt fails for any reason -- never silently leaves the baseline
    unreset.

    Real bug fixed 2026-08-01, found via the first live batch to
    actually earn and apply real patch_cpu_limit fixes at volume: this
    check used to read the CURRENT limit from the Deployment spec, but
    patch_cpu_limit's real fix (above) only ever resizes the live POD
    in place and deliberately never touches the Deployment spec -- so
    the Deployment spec always still read the original 300m baseline
    even after a real fix had pushed the live pod to 1000m/800m,
    silently skipping the reset every time. Confirmed against 3 real
    episodes this went undetected: injector's own weaker verification
    bar (CPU_THROTTLE_MIN_PERIODS_INCREASE=50) still called the
    injection "verified" against the stale, unreset limit, but the
    stressor barely throttled a 1000m-limited pod, producing a real but
    much weaker signal (66-76 periods vs. the normal 500-750+) that
    fell below agent.py's diagnosis threshold -- 3 consecutive real
    false negatives, one of them costing cpu-throttling a 39-episode
    trust streak. Fixed by reading the live POD's own resources (the
    same source patch_cpu_limit itself writes to), not the Deployment's."""
    pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
    current_limit = ""
    if pod_name is not None:
        result = subprocess.run(
            [
                "kubectl", "get", "pod", pod_name, "-n", cfg["namespace"],
                "-o", f'jsonpath={{.spec.containers[?(@.name=="{cfg["container"]}")].resources.limits.cpu}}',
            ],
            capture_output=True, text=True,
        )
        current_limit = result.stdout.strip()
    if current_limit == CPU_THROTTLE_BASELINE_CPU_LIMIT:
        return

    print(f"  {cfg['target']}'s CPU limit is {current_limit or '(unknown)'}, not the "
          f"{CPU_THROTTLE_BASELINE_CPU_LIMIT} baseline -- resetting before injecting "
          f"(a prior real fix likely raised it)...")

    if pod_name is not None:
        resources_result = subprocess.run(
            [
                "kubectl", "get", "pod", pod_name, "-n", cfg["namespace"],
                "-o", f'jsonpath={{.spec.containers[?(@.name=="{cfg["container"]}")].resources}}',
            ],
            capture_output=True, text=True,
        )
        try:
            resources = json.loads(resources_result.stdout.strip())
            resources.setdefault("limits", {})["cpu"] = CPU_THROTTLE_BASELINE_CPU_LIMIT
            resize_body = json.dumps({
                "spec": {"containers": [{"name": cfg["container"], "resources": resources}]}
            })
            resize_result = subprocess.run(
                [
                    "kubectl", "patch", "pod", pod_name, "-n", cfg["namespace"],
                    "--subresource", "resize", "--type=merge", "-p", resize_body,
                ],
                capture_output=True, text=True,
            )
            if resize_result.returncode == 0:
                print(f"  reset via in-place resize (no restart needed), pod={pod_name}")
                return
            print(f"  in-place resize failed ({resize_result.stderr.strip()}), "
                  f"falling back to a full Deployment patch + rollout wait...")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  couldn't parse pod resources for in-place resize ({e}), "
                  f"falling back to a full Deployment patch + rollout wait...")

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
    condition).

    Real bug found 2026-08-06, live: also resets oom's own baseline
    (memory limit) here, symmetric to the fix in main()'s oom branch --
    oom and under-provisioned-replicas share catalogue as their target,
    and a real prior oom fix leaves the memory limit raised with
    nothing else to revert it, same "prior real fix, nothing reverts
    it" reasoning _ensure_oom_baseline's own docstring already states.
    Passes FAULT_CONFIG["oom"] explicitly, not this function's own cfg
    -- under-provisioned-replicas' FAULT_CONFIG entry has no
    "container" key, which _ensure_oom_baseline requires."""
    _ensure_catalogue_replica_baseline(cfg)
    _ensure_oom_baseline(FAULT_CONFIG["oom"])
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


def _interruptible_sleep(duration_s: float, stop_file: str | None) -> bool:
    """Sleeps up to duration_s in ~5s ticks, checking stop_file each
    tick -- returns True if interrupted early (stop_file appeared),
    False if the full duration elapsed naturally. Shared by every
    report-only class's own 'hold for the remainder of duration_s' step
    (Operator's early-exit extension, mirroring crash-loop/
    cpu-throttling's own poll-based hold). Never called with stop_file
    set for a batch run -- omitted entirely, this degrades to a plain
    full sleep."""
    elapsed = 0.0
    tick = 5.0
    while elapsed < duration_s:
        if stop_file is not None and os.path.exists(stop_file):
            return True
        this_tick = min(tick, duration_s - elapsed)
        time.sleep(this_tick)
        elapsed += this_tick
    return False


def _write_evidence_file_once(evidence_file: str | None) -> None:
    """Called the moment a report-only class's own real verification
    first confirms the fault landed -- writes a real file (idempotent,
    a re-write is harmless) that Operator's wrapper thread polls for
    instead of re-running the same probe/mysql-exec/etc a second time
    in parallel (several of these are active, real-cost checks --
    spinning a throwaway pod, an actual mysql connection attempt --
    re-running them redundantly from the wrapper would double real load
    and risk skewing the very signal being measured). None (every batch
    run) makes this a no-op."""
    if evidence_file is not None:
        Path(evidence_file).write_text(str(time.time()))


def _inject_and_verify_init_failure(cfg: dict, stop_file: str | None = None, evidence_file: str | None = None) -> bool:
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
            _write_evidence_file_once(evidence_file)
            remaining = cfg["duration_s"] - (time.time() - window_start)
            if remaining > 0:
                _interruptible_sleep(remaining, stop_file)
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


def _inject_and_verify_session_cart_failure(
    cfg: dict, stop_file: str | None = None, evidence_file: str | None = None
) -> bool:
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
            _write_evidence_file_once(evidence_file)
            remaining = cfg["duration_s"] - (time.time() - window_start)
            if remaining > 0:
                _interruptible_sleep(remaining, stop_file)
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
    """UPSERT, not a plain INSERT -- Operator's async wrapper (Phase 1
    item 5) pre-creates a row for this exact episode_id (via --episode-id
    below) BEFORE this function ever runs, with t0/chaos_resource_name
    still NULL, so it has a real DB-backed row to attach live state to
    while injection is still in progress. This UPSERT is what fills in
    the real values once they're genuinely known -- ON CONFLICT covers
    that case, the plain INSERT path covers today's batch behavior
    (no pre-existing row, --episode-id omitted) unchanged. injector.py
    otherwise stays completely unaware of episode_state/holding/
    awaiting_fix/etc -- this is the one, narrow point of contact with
    the wrapper's own row (Kimi review 35/36's locked ownership split)."""
    conn.execute(
        "INSERT INTO episodes (episode_id, fault_class, target, namespace, t0, chaos_resource_name) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(episode_id) DO UPDATE SET "
        "fault_class=excluded.fault_class, target=excluded.target, "
        "namespace=excluded.namespace, t0=excluded.t0, "
        "chaos_resource_name=excluded.chaos_resource_name",
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
            # Real gap found 2026-07-31: Kubernetes does NOT auto-delete
            # an evicted pod's Failed object (same "lingers indefinitely"
            # behavior agent.py's own oom_query/evicted_query docstrings
            # already document) -- 17 leftover Failed queue-master pods
            # accumulated across one overnight batch's 15 real disk-full
            # episodes.
            #
            # REAL REGRESSION found and reverted the same day, live-
            # tested: the first fix attempt deleted baseline_pod_name
            # (the evicted pod) IMMEDIATELY here, right after injector-
            # side verification succeeds. That's too early -- the
            # scorer's own /diagnose call runs AFTER this function
            # returns, and its evicted_query needs THIS SAME pod object
            # to still exist (kube_pod_status_reason{reason="Evicted"})
            # to correctly diagnose disk-full at all. Deleting it here
            # removed the evidence before diagnosis ever ran, confirmed
            # live: a real disk-full episode came back "no anomaly
            # detected" (evicted_pods: []) and wrongly demoted a real
            # streak-20 can_act class. Cleanup is now deferred to
            # _ensure_queue_master_pod_cleanup, called right before the
            # NEXT disk-full injection -- same "reset before injecting,
            # not immediately after fixing" pattern _ensure_oom_baseline/
            # _ensure_cpu_throttle_baseline already use, for exactly this
            # reason (the diagnosis step needs to see the evidence in
            # between).
            return True
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no eviction/pod-churn detected{suffix}")
    return False


def _inject_and_verify_crash_loop(cfg: dict, stop_file: str | None = None) -> bool:
    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_restarts = _restart_count(cfg["target"], cfg["namespace"])
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: running exec-based kill loop for {cfg['duration_s']}s...")
        interrupted = run_crash_loop_injection(cfg, cfg["duration_s"], stop_file=stop_file)
        verified = _verify_crash_loop_effect(cfg["target"], cfg["namespace"], baseline_restarts)
        if verified:
            return True
        if interrupted:
            # A user-requested early stop (Operator's stop-file), not a
            # failed attempt -- honor it, don't retry against their
            # explicit request just because this one attempt's own
            # verification came back empty.
            print("  early-stop requested -- not retrying")
            return False
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no restart detected{suffix}")
    return False


def _inject_and_verify_network_latency(
    cfg: dict, stop_file: str | None = None, evidence_file: str | None = None
) -> str | None:
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
        interrupted = False
        elapsed = 0
        try:
            while elapsed < cfg["duration_s"]:
                if stop_file is not None and os.path.exists(stop_file):
                    interrupted = True
                    break
                time.sleep(10)
                elapsed += 10
                during_ms = _probe_orders_latency_ms(namespace)
                if during_ms is not None and during_ms >= baseline_ms + NETWORK_LATENCY_MIN_INCREASE_MS:
                    if not verified:
                        _write_evidence_file_once(evidence_file)
                    verified = True
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)

        if interrupted:
            if verified:
                return chaos_name
            print("  early-stop requested -- not retrying")
            return None

        if verified:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no latency increase observed (baseline={baseline_ms}ms){suffix}")
    return None


def _inject_and_verify_network_partition(
    cfg: dict, stop_file: str | None = None, evidence_file: str | None = None
) -> str | None:
    """Verified via _probe_orders_reachable (direct probe, NOT k6/
    Prometheus -- see NETWORK_PARTITION_PROBE_SAMPLES's docstring for
    why k6_http_req_failed is unusable here: front-end's own call to
    orders has no timeout and hangs indefinitely rather than ever
    failing observably while the partition holds).

    Probes once early (after a propagation wait) to confirm the block
    landed for real, then holds the fault for its FULL duration_s --
    same "don't end early" discipline as network-latency, disk-full,
    memory-leak, and connection-pool-exhaustion all independently
    learned the hard way: ending a fault the instant our own probe is
    satisfied starves any other real observer (traffic_gen, a future
    real diagnosis query) of a fair chance to see it too.
    Deliberately does NOT probe again near the end of the window --
    confirmed during measurement (2026-07-24) that a probe landing right
    at the CR's natural expiry boundary can show a false partial
    recovery purely from probe-pod scheduling overhead, not a real
    leaky block.

    Propagation wait bumped 5s -> 25s (2026-07-28): the class started
    failing verification 3/3 attempts despite the block mechanism itself
    being genuinely healthy (confirmed via direct manual repro -- a real
    egress connection to an external IP timed out during an active
    partition, and chaos-daemon's logs showed the iptables rule applying
    cleanly). Re-running measure_network_partition_direction_check.py
    (the original 2026-07-24 measurement script) showed the real
    timeline: baseline ~2100-2600 bytes/s tx/rx, still a leaky
    30-1250 bytes/s at t+10s/t+20s (NOT a clean block yet), only
    reliably near-zero from t+30-40s onward. A 5s wait landed the probe
    squarely in that leaky transitional window, not because the class'
    real signal is unreliable -- agent.py's own diagnosis-side
    min_over_time(...[2m]) window comfortably outlasts this settle time
    regardless, which is why a real episode's diagnosis was never
    actually broken by this, only the injector's own tighter,
    single-early-probe self-verification was."""
    chaos_kind = "networkchaos"
    namespace = cfg["namespace"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        chaos_name = f"{cfg['chaos_name_prefix']}-{uuid.uuid4().hex[:8]}"
        manifest = build_network_partition_manifest(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: applied, waiting 25s for propagation "
              f"before probing...")

        verified = False
        window_start = time.time()
        try:
            time.sleep(25)
            failures = _probe_orders_reachable(namespace)
            verified = failures >= NETWORK_PARTITION_MIN_FAILURES
            print(f"  early probe: {failures}/{NETWORK_PARTITION_PROBE_SAMPLES} samples failed "
                  f"(need >= {NETWORK_PARTITION_MIN_FAILURES})")
            if verified:
                _write_evidence_file_once(evidence_file)

            # Sleep out whatever's genuinely left of duration_s, based on
            # REAL elapsed wall-clock time (the 5s wait + the probe's own
            # real scheduling/curl overhead), not an assumed constant --
            # the exact lesson this class's own measurement scripts
            # taught about probe overhead eating into fault windows.
            remaining = cfg["duration_s"] - (time.time() - window_start)
            if remaining > 0:
                _interruptible_sleep(remaining, stop_file)
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)

        if verified:
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: partition did not verify{suffix}")
    return None


def _inject_and_verify_memory_leak(
    cfg: dict, stop_file: str | None = None, evidence_file: str | None = None
) -> str | None:
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
        interrupted = False
        peak_mib = baseline_mib
        evidence_written = False
        elapsed = 0
        try:
            while elapsed < cfg["duration_s"]:
                if stop_file is not None and os.path.exists(stop_file):
                    interrupted = True
                    break
                time.sleep(10)
                elapsed += 10
                current_mib = _memory_working_set_mib(target, namespace, container)
                if current_mib is not None:
                    peak_mib = max(peak_mib, current_mib)
                if not evidence_written and peak_mib >= baseline_mib + MEMORY_LEAK_MIN_INCREASE_MIB:
                    _write_evidence_file_once(evidence_file)
                    evidence_written = True
                if _restart_count(target, namespace) > baseline_restarts:
                    restarted = True
                    break  # stop early -- this is an OOM now, not a leak, no point holding further
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)

        if interrupted:
            if evidence_written:
                return chaos_name
            print("  early-stop requested -- not retrying")
            return None

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


def _ensure_flood_user(cfg: dict) -> None:
    """Re-creates the floodtest MySQL user idempotently before every
    flood attempt. Found the hard way (2026-07-27): catalogue-db has no
    persistent volume, so any container restart (which happens routinely
    -- e.g. a cluster-wide WSL2/k3s restart) silently wipes MySQL back to
    a fresh state, deleting this manually-created user along with it.
    create_connection_pool_flood_user.sh was originally treated as a
    one-time setup step, which is wrong given that restart behavior --
    every future flood attempt would otherwise fail silently (the
    backgrounded mysql client processes just error out with auth
    failures, stderr redirected to /dev/null) with no real connections
    ever established. CREATE USER IF NOT EXISTS makes this safe to call
    unconditionally on every attempt, same self-healing pattern already
    used elsewhere in this file (_ensure_cpu_throttle_baseline,
    _ensure_front_end_image_baseline)."""
    namespace = cfg["namespace"]
    container = cfg["container"]
    pod_name = _current_pod_name(cfg["target"], namespace)
    if pod_name is None:
        return
    subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "mysql", "-uroot", "-pfake_password", "-e",
            f"CREATE USER IF NOT EXISTS '{CONNECTION_POOL_FLOOD_USER}'@'%' "
            f"IDENTIFIED BY '{CONNECTION_POOL_FLOOD_PASSWORD}'; "
            f"GRANT USAGE ON *.* TO '{CONNECTION_POOL_FLOOD_USER}'@'%'; "
            f"FLUSH PRIVILEGES;",
        ],
        capture_output=True, text=True, timeout=15,
    )


def _get_catalogue_db_threads_connected(cfg: dict) -> int | None:
    """Real, LIVE baseline measurement -- queries MySQL's own real
    Threads_connected status variable right before flooding, rather
    than trusting a hand-tuned static assumption. Real history: this
    baseline has drifted upward repeatedly (2-3 -> 7 as of 2026-07-25,
    and again by the 2026-07-29/30 overnight run that surfaced this
    fix), silently thinning CONNECTION_POOL_FLOOD_CONNECTIONS's real
    margin each time until a flood attempt started failing. Measuring
    live every attempt, instead of re-guessing a new static number
    after the fact, closes this recurring gap for good. Returns None
    if the pod isn't reachable or the query fails -- callers fall back
    to the historical-floor constant rather than erroring out."""
    namespace = cfg["namespace"]
    container = cfg["container"]
    pod_name = _current_pod_name(cfg["target"], namespace)
    if pod_name is None:
        return None
    result = subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "mysql", "-uroot", "-pfake_password", "-N", "-e",
            "SHOW STATUS LIKE 'Threads_connected';",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f"  Threads_connected query failed (pod={pod_name}): "
              f"returncode={result.returncode} stderr={result.stderr.strip()[:300]!r}")
        return None
    try:
        return int(result.stdout.strip().split()[1])
    except (IndexError, ValueError):
        print(f"  Threads_connected query returned unparseable output: {result.stdout!r}")
        return None


def _compute_flood_target(cfg: dict) -> int:
    """Real, LIVE-measured flood size, replacing the old static
    CONNECTION_POOL_FLOOD_CONNECTIONS=170 guess that needed manual
    re-tuning three separate times (140 -> 150 -> 170) purely because
    real baseline Threads_connected kept drifting upward between
    measurements. Sizing the flood off of a fresh live reading every
    attempt means this never goes stale again, regardless of how much
    the baseline grows with future accumulated testing."""
    baseline = _get_catalogue_db_threads_connected(cfg)
    if baseline is None:
        print("  could not read a live Threads_connected baseline -- "
              f"falling back to the historical floor ({CONNECTION_POOL_FLOOD_CONNECTIONS})")
        return CONNECTION_POOL_FLOOD_CONNECTIONS
    needed_to_fill = CATALOGUE_DB_MAX_CONNECTIONS - baseline
    target = int((needed_to_fill + CONNECTION_POOL_SAFETY_MARGIN) / CONNECTION_POOL_ESTABLISH_SUCCESS_RATE) + 1
    # Never go BELOW the known-historically-working floor, even if a
    # low live baseline reading would otherwise suggest a smaller flood.
    return max(target, CONNECTION_POOL_FLOOD_CONNECTIONS)


def _flood_connections(cfg: dict) -> bool:
    """Single kubectl exec into catalogue-db backgrounds a live-computed
    number of real mysql client processes (see _compute_flood_target,
    each holding a genuine connection open via SELECT SLEEP), then
    returns immediately once the loop finishes issuing them -- the
    backgrounded children keep running inside the container after this
    exec session ends (no Chaos Mesh involved at all; there's no
    primitive for this, so this is a direct real mechanism like
    crash-loop/disk-full). Real capacity consumed against MySQL's own
    max_connections (151, confirmed empirically), not simulated."""
    namespace = cfg["namespace"]
    container = cfg["container"]
    target = cfg["target"]
    pod_name = _current_pod_name(target, namespace)
    if pod_name is None:
        return False
    _ensure_flood_user(cfg)
    flood_target = _compute_flood_target(cfg)
    print(f"  live-measured flood target: {flood_target} connections")
    sleep_s = cfg["duration_s"] + 15  # outlives our own polling window with margin
    script = (
        f"for i in $(seq 1 {flood_target}); do "
        f'mysql -u{CONNECTION_POOL_FLOOD_USER} -p{CONNECTION_POOL_FLOOD_PASSWORD} '
        f'-e "SELECT SLEEP({sleep_s})" '
        f">/dev/null 2>&1 & done"
    )
    result = subprocess.run(
        ["kubectl", "exec", "-n", namespace, pod_name, "-c", container, "--", "sh", "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"  flood exec failed (pod={pod_name}): returncode={result.returncode} "
              f"stderr={result.stderr.strip()[:300]!r}")
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


def _restart_catalogue_db_pod(cfg: dict, timeout_s: int = 90) -> bool:
    """Real self-healing step, added 2026-07-30 after a real overnight
    run left catalogue-db unable to exec at all (OCI runtime error
    'unable to spawn stage-1: Resource temporarily unavailable' --
    a real process/PID-limit exhaustion signature, confirmed live).
    Real root cause: the flood mechanism backgrounds many real mysql
    client processes per attempt; across enough repeated attempts in
    one run, some can outlive their own cleanup, and once the container
    actually hits its process limit, _cleanup_connection_flood's own
    exec (the pkill that would normally clear them) fails for the exact
    same reason -- a genuine chicken-and-egg problem a human had to
    break manually (`kubectl delete pod`) before this fix existed.

    Safe to do automatically: catalogue-db has no persistent volume
    (see _ensure_flood_user's docstring), so a restart wipes it to a
    known-clean state, and flood-user creation is already idempotent
    specifically to survive exactly this. Deletes the current pod and
    polls _current_pod_name (which only ever returns a Running pod)
    until a genuinely NEW one is up, rather than assuming a fixed
    sleep is long enough.

    Real fix, 2026-08-03 (self-heal auth race, found live 2026-08-02
    overnight batch): 'Running' only means the container process
    started, not that MySQL inside it is ready to authenticate root
    yet -- a real 'Access denied for user root@localhost' was observed
    on the very next _get_catalogue_db_threads_connected call right
    after this function returned True, silently thinning the flood via
    the historical-floor fallback instead of a real live reading.
    Added a real MySQL-readiness poll (a plain 'SELECT 1' as root, the
    same auth path _get_catalogue_db_threads_connected itself needs)
    after the pod is Running, before declaring self-heal complete."""
    namespace = cfg["namespace"]
    container = cfg["container"]
    target = cfg["target"]
    old_pod_name = _current_pod_name(target, namespace)
    print(f"  self-heal: restarting catalogue-db pod ({old_pod_name or 'unknown'}) to clear "
          f"a real process-limit exhaustion...")
    if old_pod_name:
        subprocess.run(
            ["kubectl", "delete", "pod", "-n", namespace, old_pod_name, "--wait=false"],
            capture_output=True, text=True, timeout=30,
        )
    deadline = time.time() + timeout_s
    new_pod_name = None
    while time.time() < deadline:
        time.sleep(5)
        candidate = _current_pod_name(target, namespace)
        if candidate and candidate != old_pod_name:
            new_pod_name = candidate
            print(f"  self-heal: catalogue-db back up as {new_pod_name}, "
                  f"waiting for MySQL root auth to be ready...")
            break
    if new_pod_name is None:
        print(f"  self-heal: catalogue-db did not come back Running within {timeout_s}s")
        return False

    while time.time() < deadline:
        result = subprocess.run(
            ["kubectl", "exec", "-n", namespace, new_pod_name, "-c", container,
             "--", "mysql", "-uroot", "-pfake_password", "-e", "SELECT 1"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            print(f"  self-heal: MySQL root auth ready on {new_pod_name}")
            return True
        time.sleep(3)
    print(f"  self-heal: catalogue-db pod up but MySQL root auth never became ready "
          f"within {timeout_s}s -- proceeding anyway, caller will fall back to the "
          f"historical floor if the next live read still fails")
    return True


def _inject_and_verify_connection_pool_exhaustion(
    cfg: dict, stop_file: str | None = None, evidence_file: str | None = None
) -> str | None:
    """Verified by actually attempting one more real connection and
    confirming it fails with MySQL's genuine 'too many connections'
    error -- the same thing catalogue itself would experience, not an
    inferred threshold. Holds for the full duration_s every attempt,
    same fix applied to network-latency/memory-leak after learning the
    hard way that ending a fault early starves the agent's later
    diagnosis query of a fair chance to see it."""
    duration_s = cfg["duration_s"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: flooding catalogue-db "
              f"(live-measured target), holding for the full {duration_s}s window...")
        flooded = _flood_connections(cfg)
        if not flooded:
            print(f"  attempt {attempt}: failed to launch the connection flood "
                  f"(pod not found / exec error) -- likely a real process-limit "
                  f"exhaustion (see _restart_catalogue_db_pod's docstring)")
            _restart_catalogue_db_pod(cfg)
            if attempt < MAX_INJECT_ATTEMPTS:
                print("  retrying now that catalogue-db has been restarted")
            continue

        try:
            time.sleep(5)  # give the flood a moment to actually establish all connections
            verified = _test_connection_fails(cfg)
            if verified:
                _write_evidence_file_once(evidence_file)
            remaining = duration_s - 5
            if remaining > 0:
                _interruptible_sleep(remaining, stop_file)
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


def _inject_and_verify_cpu_throttling(cfg: dict, stop_file: str | None = None) -> str | None:
    """StressChaos cpu stressor against `user`, held for the full
    duration_s (same 'don't end early' discipline every other class
    learned the hard way -- an external observer, or a future real
    diagnosis call, needs a fair chance to see the fault too). Verified
    via a raw before/after delta on container_cpu_cfs_throttled_periods_total,
    not Chaos Mesh's own state.

    stop_file: same early-exit contract as run_crash_loop_injection --
    checked once per ~10s tick during the hold; omit for batch runs."""
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
        interrupted = False
        elapsed = 0
        try:
            while elapsed < cfg["duration_s"]:
                if stop_file is not None and os.path.exists(stop_file):
                    interrupted = True
                    break
                time.sleep(10)
                elapsed += 10
            # Verified INSIDE the same try as the hold, before cleanup --
            # matches every other class's own established pattern
            # (network-latency, connection-pool-exhaustion above), not a
            # new ordering. The counter being checked
            # (container_cpu_cfs_throttled_periods_total) is a
            # non-resetting kernel cgroup counter, so checking it before
            # vs. after the chaos resource itself is deleted makes no
            # real difference to the reading -- kept ordered this way
            # purely for consistency with the rest of this file.
            verified = _verify_cpu_throttle_effect(target, namespace, container, baseline_periods)
        finally:
            delete_chaos_resource(chaos_kind, chaos_name)
        if verified:
            return chaos_name
        if interrupted:
            # A user-requested early stop, not a failed attempt -- honor
            # it, don't retry against their explicit request just
            # because this one attempt's own verification came back
            # empty.
            print("  early-stop requested -- not retrying")
            return None
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no real throttled-periods increase observed{suffix}")
    return None


def _pod_restart_count_direct(pod_name: str, namespace: str, container: str) -> int | None:
    """Direct k8s API restartCount, via kubectl -- sub-second, no
    scrape lag (unlike Prometheus). Returns None if unparseable (pod
    gone, field missing)."""
    result = subprocess.run(
        [
            "kubectl", "get", "pod", pod_name, "-n", namespace,
            "-o", f'jsonpath={{.status.containerStatuses[?(@.name=="{container}")].restartCount}}',
        ],
        capture_output=True, text=True,
    )
    value = result.stdout.strip()
    return int(value) if value.isdigit() else None


def _pod_oom_killed(pod_name: str, namespace: str, container: str, baseline_restart_count: int) -> bool:
    """Direct k8s API check via kubectl -- sub-second, no scrape lag,
    unlike polling a Prometheus counter (kube_pod_container_status_restarts_total
    is only as fresh as kube-state-metrics' own scrape cycle). Checks
    the SAME field this bug was root-caused with manually, 2026-08-01
    (kubectl ... lastState.terminated), via Kimi review 19.

    Real bug fixed same day, found live on the very first real test:
    checking lastState.terminated.reason alone is NOT enough --
    lastState reflects the PREVIOUS container instance's exit reason
    and stays populated until the NEXT restart overwrites it, so a
    stale OOMKilled from an earlier, unrelated kill was read as fresh
    on the very first poll (elapsed=0s -- physically impossible for a
    real memory-pressure kill), falsely verifying an episode that
    hadn't actually landed yet and costing a real 10-episode trust
    streak. Fixed by requiring restartCount to have genuinely
    increased past a baseline captured before the stressor was
    applied, in addition to the reason check -- same delta-not-
    presence principle the old Prometheus-based _verify_restart_effect
    already got right, just re-applied here against the direct k8s
    field instead of losing it in the scrape-lag fix."""
    current = _pod_restart_count_direct(pod_name, namespace, container)
    if current is None or current <= baseline_restart_count:
        return False
    result = subprocess.run(
        [
            "kubectl", "get", "pod", pod_name, "-n", namespace,
            "-o", f'jsonpath={{.status.containerStatuses[?(@.name=="{container}")].lastState.terminated.reason}}',
        ],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "OOMKilled"


def _inject_and_verify_oom(cfg: dict) -> str | None:
    """Real redesign, 2026-08-01 (Kimi review 19, reviews/19_oom_verification_race_kimi_review.md)
    -- replaces the old fixed-sleep-then-Prometheus-poll approach
    (_inject_and_verify_chaos_mesh), which failed 9 real injection
    attempts across 2 separate live batches. Root cause was two
    stacked problems, confirmed via real kubectl lastState.terminated
    data on both failures (97s and 119s real stressor-start-to-kill
    times): (1) the old code deleted the StressChaos resource after a
    FIXED sleep, before verification even started -- if the kill
    hadn't happened yet, tearing down the stressor worked against ever
    seeing it; (2) even with a longer fixed sleep, polling
    kube_pod_container_status_restarts_total via Prometheus has real
    scrape lag on top of the kernel's own non-deterministic OOM-kill
    timing, so a fixed poll window could still miss a real kill that
    landed a few seconds late.

    Fixed by merging hold-and-verify into one loop: the stressor stays
    ACTIVE while polling the k8s API directly (lastState.terminated,
    sub-second, no scrape lag) every 3s, tearing down only once
    confirmed OOMKilled or a 200s hard ceiling is hit (real margin over
    the two observed real kills of 97s/119s -- not a bigger version of
    the same guess, a backstop for a loop that's actively watching the
    real signal the whole time). Re-resolves the pod name each
    iteration in case of pod churn under memory pressure (same
    old-pod/new-pod bug class already found elsewhere in this file)."""
    chaos_kind = "stresschaos"
    namespace = cfg["namespace"]
    target = cfg["target"]
    container = cfg["container"]

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        pod_name = _current_pod_name(target, namespace)
        baseline_restart_count = (
            _pod_restart_count_direct(pod_name, namespace, container) if pod_name is not None else None
        )
        chaos_name = f"{cfg['chaos_name_prefix']}-{uuid.uuid4().hex[:8]}"
        manifest = build_oom_manifest(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: stressor active, polling for a real "
              f"OOM kill (ceiling {OOM_VERIFY_CEILING_S}s, baseline restartCount={baseline_restart_count})...")
        verified = False
        elapsed = 0
        try:
            while elapsed <= OOM_VERIFY_CEILING_S:
                current_pod_name = _current_pod_name(target, namespace)
                if (
                    current_pod_name is not None
                    and baseline_restart_count is not None
                    and _pod_oom_killed(current_pod_name, namespace, container, baseline_restart_count)
                ):
                    verified = True
                    break
                time.sleep(OOM_VERIFY_POLL_S)
                elapsed += OOM_VERIFY_POLL_S
        finally:
            # Cleanup must run even if the poll loop throws -- an
            # active memory stressor left behind would keep pressuring
            # (and potentially OOM-killing) the target indefinitely.
            delete_chaos_resource(chaos_kind, chaos_name)
        if verified:
            print(f"  attempt {attempt}: real OOMKilled confirmed after ~{elapsed}s")
            return chaos_name
        suffix = ", retrying" if attempt < MAX_INJECT_ATTEMPTS else ""
        print(f"  attempt {attempt}: no OOM kill detected within {OOM_VERIFY_CEILING_S}s{suffix}")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--class", dest="fault_class", required=True, choices=FAULT_CONFIG.keys())
    parser.add_argument(
        "--duration-override", dest="duration_override", type=int, default=None,
        help=(
            "Override this class's FAULT_CONFIG duration_s for this run only -- "
            "does NOT mutate FAULT_CONFIG (a fresh per-run copy is used). Exists "
            "so the real production CLI path can be exercised at extended "
            "durations (e.g. Operator's locked 180s/300s numbers) instead of "
            "only via one-off scripts calling internal functions directly."
        ),
    )
    parser.add_argument(
        "--episode-id", dest="episode_id", default=None,
        help=(
            "Operator's async wrapper (Phase 1 item 5, Kimi review 36's "
            "locked Option A) pre-creates the episodes row under this ID "
            "before this script ever runs, so it has a real DB-backed "
            "row to attach live episode_state to during injection. "
            "record_episode() UPSERTs into that existing row instead of "
            "INSERTing a fresh one. Omit for batch runs (run_batch_plan.py "
            "never passes this) -- a fresh UUID is generated as today."
        ),
    )
    parser.add_argument(
        "--stop-file", dest="stop_file", default=None,
        help=(
            "Operator's early-exit signal, all 8 extendable classes "
            "(crash-loop/cpu-throttling from Kimi review 36 findings 2/7, "
            "plus the 6 report-only classes) -- the hold loop checks "
            "os.path.exists(stop_file) once per tick and exits cleanly, "
            "running its own finally-block cleanup, if the file appears. "
            "Ignored by disk-full/under-provisioned-replicas/bad-rollout/"
            "oom (no extendable hold to interrupt). Omit for batch runs."
        ),
    )
    parser.add_argument(
        "--evidence-file", dest="evidence_file", default=None,
        help=(
            "Report-only classes only -- written the moment this "
            "script's own real verification first confirms the fault "
            "landed, so Operator's wrapper can unlock an early-stop "
            "button without re-running the same active probe/mysql-exec "
            "a second time in parallel. Ignored by every other class "
            "(crash-loop/cpu-throttling's evidence check is a cheap "
            "Prometheus read the wrapper re-runs independently instead). "
            "Omit for batch runs."
        ),
    )
    args = parser.parse_args()

    fault_class = args.fault_class
    cfg = dict(FAULT_CONFIG[fault_class])
    if args.duration_override is not None:
        print(
            f"  duration_s override: {cfg['duration_s']}s -> {args.duration_override}s "
            f"(FAULT_CONFIG itself unchanged)"
        )
        cfg["duration_s"] = args.duration_override

    episode_id = args.episode_id if args.episode_id is not None else str(uuid.uuid4())
    t0 = datetime.now(timezone.utc).isoformat()

    # Real cross-process lock -- see acquire_system_lock_or_die's own
    # docstring/comment block above. Acquired before ANY real cluster
    # mutation and released in the finally below regardless of how this
    # run ends (success, a refused injection, or an exception).
    lock_holder = f"pid={os.getpid()} class={fault_class} started={t0}"
    acquire_system_lock_or_die(lock_holder)
    try:
        # Real, live-verified fix, 2026-08-01 -- see _clear_stale_oom_sticky_flag's
        # own docstring for the full incident. Placed HERE, in main(), not
        # inside _ensure_oom_baseline (which only run_batch_plan.py's own
        # BASELINE_CHECKS ever calls) -- this is the one real entry point
        # EVERY trigger path shares (run_batch_plan.py's subprocess call,
        # AND operator_api.py's live-trigger subprocess call), so a fix
        # placed here applies universally regardless of who or what
        # triggered this specific episode. Cheap no-op in the common case.
        _clear_stale_oom_sticky_flag(cfg)

        print(f"Episode {episode_id}: attempting {fault_class} on {cfg['target']} ({cfg['namespace']}) at {t0}")

        if fault_class == "disk-full":
            _ensure_queue_master_pod_cleanup(cfg)
            verified = _inject_and_verify_disk_full(cfg)
            chaos_name = "manual-exec" if verified else None
        elif fault_class == "crash-loop":
            verified = _inject_and_verify_crash_loop(cfg, stop_file=args.stop_file)
            chaos_name = "manual-exec" if verified else None
        elif fault_class == "network-latency":
            chaos_name = _inject_and_verify_network_latency(
                cfg, stop_file=args.stop_file, evidence_file=args.evidence_file
            )
        elif fault_class == "memory-leak":
            chaos_name = _inject_and_verify_memory_leak(
                cfg, stop_file=args.stop_file, evidence_file=args.evidence_file
            )
        elif fault_class == "connection-pool-exhaustion":
            chaos_name = _inject_and_verify_connection_pool_exhaustion(
                cfg, stop_file=args.stop_file, evidence_file=args.evidence_file
            )
        elif fault_class == "network-partition":
            chaos_name = _inject_and_verify_network_partition(
                cfg, stop_file=args.stop_file, evidence_file=args.evidence_file
            )
        elif fault_class == "init-failure":
            verified = _inject_and_verify_init_failure(
                cfg, stop_file=args.stop_file, evidence_file=args.evidence_file
            )
            chaos_name = "manual-patch" if verified else None
        elif fault_class == "session-cart-failure":
            verified = _inject_and_verify_session_cart_failure(
                cfg, stop_file=args.stop_file, evidence_file=args.evidence_file
            )
            chaos_name = "manual-scale" if verified else None
        elif fault_class == "cpu-throttling":
            chaos_name = _inject_and_verify_cpu_throttling(cfg, stop_file=args.stop_file)
        elif fault_class == "under-provisioned-replicas":
            chaos_name = _inject_and_verify_under_provisioned(cfg)
        elif fault_class == "bad-rollout":
            verified = _inject_and_verify_bad_rollout(cfg)
            chaos_name = "manual-patch" if verified else None
        elif fault_class == "oom":
            # Real bug found 2026-08-06, live: oom and under-provisioned-
            # replicas share the same target (catalogue), but each class's
            # own baseline-reset function only ever touched ITS OWN
            # dimension (memory limit here, replica count in
            # _ensure_catalogue_replica_baseline) -- neither reset the
            # OTHER'S. A real UPR fix that scaled catalogue to 3 replicas
            # left it there for the next oom episode, and Chaos Mesh's
            # `mode: one` StressChaos selector can then pick a DIFFERENT
            # one of the 3 pods than the one _current_pod_name()/the
            # verification poll is watching -- 3 consecutive real injection
            # failures, confirmed live (kubectl showed replicas=3 mid-batch).
            # Resetting both dimensions before injecting either class now.
            _ensure_catalogue_replica_baseline(cfg)
            _ensure_oom_baseline(cfg)
            chaos_name = _inject_and_verify_oom(cfg)
        else:
            # Unreachable in practice -- argparse's choices=FAULT_CONFIG.keys()
            # and every real key above already has its own explicit branch.
            raise ValueError(f"no injection mechanism wired up for fault_class={fault_class!r}")

        if not chaos_name:
            print(
                f"INJECTION FAILED after {MAX_INJECT_ATTEMPTS} attempts for {fault_class} on "
                f"{cfg['target']} -- NO episode recorded. If this keeps happening, the cluster "
                f"(or Chaos Mesh's own daemon) is unhealthy, not the diagnosis/verifier code."
            )
            # Real bug, found via Kimi review 36 finding 1: this used to
            # be a bare `return`, which exits main() -- and therefore the
            # whole process -- with code 0. A caller checking returncode
            # alone (which Operator's new async wrapper does, since
            # UPSERT-based row pre-creation means a plain "no episode
            # exists" check isn't available anymore) would misread total
            # injection failure as success. run_batch_plan.py never hit
            # this (it only ever checks "did an unscored episode row
            # appear," which already correctly stayed empty on failure)
            # -- this was a latent bug, not something today's batch path
            # was ever exposed to.
            sys.exit(1)

        conn = ensure_db()
        record_episode(conn, episode_id, fault_class, cfg, chaos_name, t0)
        conn.close()
        print(f"Episode {episode_id}: injection verified ({chaos_name}) and ground truth recorded.")
    finally:
        lock_conn = ensure_db()
        _release_system_lock(lock_conn, lock_holder)
        lock_conn.close()


if __name__ == "__main__":
    main()
