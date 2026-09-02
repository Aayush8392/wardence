package wardence;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.lang.instrument.Instrumentation;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
// Real post-GC heap tracking (review 57, Qwen's catch): Runtime.totalMemory() -
// freeMemory() includes transient young-gen garbage, so it is NOT the live set and is
// the wrong thing to compare a hard ceiling against -- ordinary request churn can spike
// it several MiB between collections. A GC notification listener gives the real
// post-collection heap usage, which is the honest "how much is genuinely retained"
// number, and also finally settles this session's unexplained floor swings (27-95MiB
// snapshot readings that were never comparable to each other).
import java.lang.management.MemoryUsage;
import java.util.Map;
import javax.management.MBeanServer;
import javax.management.Notification;
import javax.management.NotificationEmitter;
import javax.management.NotificationListener;
import javax.management.ObjectName;
import javax.management.openmbean.CompositeData;
import com.sun.management.GarbageCollectionNotificationInfo;

// Hardened rebuild per review 55 (Kimi + Qwen, both consulted, both
// converged on splitting responsibilities across independent threads
// with a heartbeat separate from the main status/control path). Real
// root cause of the single-threaded version's concurrency=50 freeze is
// NOT confirmed (CPU cgroup starvation per Kimi, possibly also disk I/O
// or JVM/safepoint stall per Qwen) -- this design defends against all
// three rather than betting on one theory.
public class LeakAgent {
    // ---- retained allocation state (owned by the worker thread) ----
    // Real, new design (user's own direction, tested live not pre-vetted): retained
    // memory used to be independent byte[] blocks -- the cheapest possible thing for G1
    // to collect, since a pause mostly just copies bytes, never chases references. A
    // dense graph structure changes what G1 actually has to DO per pause, not how much it
    // retains -- more cross-region references means more real remembered-set (RS) update/
    // scan work every single collection, which is a genuinely different lever from every
    // target-size/load-rate/concurrency knob already tried tonight (all of which varied
    // WHAT g1 collects, never HOW EXPENSIVE collecting it is).
    // Real correctness constraint, load-bearing: refs must only ever point BACKWARD (to
    // already-created, lower-index nodes), never forward. This is what keeps RELEASE
    // correct -- governorTrimTo()/forceRelease() always remove from the END of CHUNKS
    // (the newest nodes first), and since only NEWER nodes ever reference OLDER ones, no
    // remaining (older) node can hold a reference to a just-removed (newer) node. Removed
    // nodes become genuinely, immediately unreachable -- no dangling-reference leak that
    // would make RELEASE silently fail to actually shrink retained memory.
    private static final class Node {
        final byte[] payload;
        final Node[] refs;
        Node(byte[] payload, Node[] refs) { this.payload = payload; this.refs = refs; }
    }
    // Real, deliberate count, not yet empirically tuned -- a first real attempt, per the
    // user's own "test it ourselves first" call rather than pre-vetting the exact number.
    // 8 cross-references per node is a real, non-trivial fan-out (denser than a linked
    // list's 1) without being absurd (not fully-connected, which would be O(n^2) memory
    // for the reference arrays themselves at any real retained count).
    private static final int REFS_PER_NODE = 8;
    // Real, root-caused fix (found live, both target=60 and target=30 runs aborting at
    // the SAME ~t+14s regardless of target size -- proof the danger was never about total
    // retained MiB): picking refs uniformly from ALL existing nodes means the first
    // handful of nodes (created when the pool is tiny) get referenced by nearly every
    // later node -- a runaway "hub" effect fully formed within the first couple hundred
    // node creations, independent of target. A bounded sliding window caps how many
    // incoming references any single node can ever accumulate, killing the hub effect,
    // while staying large enough (multiple regions' worth of nodes) that refs still land
    // across real region boundaries -- the actual cross-region cost this mechanism needs
    // to stay real, without the unbounded blow-up.
    private static final int REF_WINDOW_SIZE = 50;
    private static final java.util.Random GRAPH_RANDOM = new java.util.Random();
    private static final List<Node> CHUNKS = new ArrayList<Node>();
    private static final Object CHUNKS_LOCK = new Object();
    // 256 KiB chunks -- real, measured fix from review 53/the original prototype: JDK8
    // G1GC's default region size for a small ~128MB heap floors at 1MiB, and any object
    // >=50% of the region size (>=512KiB) is "humongous" and handled far less efficiently.
    private static final int CHUNK_BYTES = 256 * 1024;
    // Real, measured fix (2026-08-2x session, hardened-agent run at target=95MiB): a
    // real 97% GC-time fraction hit at heap_used=86MiB, well BEFORE reaching target and
    // well below the ~124MiB thrash onset earlier single-target-hold tests measured --
    // the tight, sleep-free allocation loop (correct fix for the ORIGINAL control-thread
    // starvation bug) ramps so fast that it spikes G1 allocation-rate pressure on its
    // own, independent of the final retained size. A real memory leak in production
    // grows over seconds/minutes anyway, never instantaneously -- pacing the ramp is
    // both the fix AND more realistic behavior, not just a workaround.
    // Real, deliberate distinction from Kimi's "worker never sleeps" rule (review 55,
    // Q1): that rule was about the OLD single-threaded design, where ANY sleep froze
    // the WHOLE agent (one thread did cmd-read/allocate/status-write/heartbeat
    // together). Here the worker is its own isolated thread -- heartbeat/control/
    // watchdog keep running independently even while the worker paces itself. A slower
    // ramp is a benign failure mode; a fully unresponsive agent (the thing actually
    // being guarded against) is not reintroduced by this.
    // DEFAULT 40 -- byte-identical to every prior run and to WSL2, which is still on the
    // small-heap/G1 config the measurement above was taken on. Made overridable
    // 2026-09-02 (-Dwardence.leak.chunkPaceMs) because the 40ms pacing does NOT transfer
    // to prod's current config: the hazard it guards against is the ramp exhausting
    // headroom before reaching target, measured at 97% GC-time by heap_used=86MiB on a
    // 128MiB G1 heap. Prod is now a 640MiB SerialGC heap with a 360MiB target, so there
    // is ~280MiB of slack the whole way up and that failure mode is not reachable.
    // What 40ms DOES cost there is real: 256KiB/40ms = ~6.25MiB/s, so a 360MiB companion
    // needs ~58s to reach target -- and the felt effect tracks the live set the entire
    // way, so most of a 180s hold ran below the swept operating point (measured live:
    // allocated_mb=77/360, post_gc_heap 290 vs ~535 at target). Prod sets 10 (=25MiB/s,
    // ~14s ramp) via patch_shipping_serialgc_leak.sh. Lower with care: this paces
    // RETAINED allocation, so a faster ramp fills old gen faster and makes the Full GCs
    // during the ramp progressively more expensive -- intended here, but it is also what
    // could trip the governor before target if pushed too far. injector.py's saturation
    // wait treats that as a warning, not an abort.
    private static final long CHUNK_PACE_MS =
        Long.getLong("wardence.leak.chunkPaceMs", 40L);

    // Continuous-growth mode (2026-08-29, -Dwardence.leak.growthMbPerSec, DEFAULT 0=off,
    // byte-identical to every prior run). A real memory leak GROWS -- it does not ramp to
    // a fixed size and stop. Once retained reaches `targetMb`, the worker keeps allocating
    // at this rate (MiB/s), growing retained memory toward ~97% of -Xmx, until RELEASE or
    // the governor's absolute post-GC-heap ceiling. Against a nearly-full old gen (small
    // -Xmx + a pinned-small young gen) every few MiB of real growth forces a Full GC that
    // cannot reclaim the leak -- continuous, escalating GC pressure, exactly what a real
    // leak does. A static "ramp then hold" leak parks in old gen and produces almost no
    // pressure once request churn stops promoting into it (measured 2026-08-29: 3 Full GCs
    // in 180s at 96%-full old gen, because nothing new was promoting).
    private static final long GROWTH_MB_PER_SEC =
        Long.getLong("wardence.leak.growthMbPerSec", 0L);

    // ---- governor (spike-and-recover leak) state, added after real 2026-08-2x data ----
    // Both Kimi and Qwen (reviews 55/56) converged on this as the real path to a
    // SUSTAINED 60s felt effect on a 128MiB heap: ramp toward target, but back off a
    // bounded amount the instant GC pressure gets severe, hold, then resume ramping once
    // it recovers -- real, repeated pressure waves instead of one early death spiral.
    // Watermarks below are grounded in what THIS session actually measured (review 57's
    // fixed, cross-validated GC signal), not the ~112-115MiB Qwen originally guessed
    // before real post-GC heap tracking existed: two independent runs (concurrency=15
    // and concurrency=8) both showed real STW-pause fraction climbing 28%->39-40% as
    // post-GC heap rose from ~64MiB to ~68-70MiB, in as little as ~8 real seconds. These
    // are a first REASONED attempt at governing that specific escalation, not yet
    // empirically validated for governed behavior -- the next real run is what tests them.
    // Real, measured tuning pass (session cont'd): 35% gave a full clean 60s hold twice
    // (70MiB, 80MiB) but only reached 3s+ latency at the extreme tail. Tried 45% next --
    // real result: the governor NEVER ENGAGED (natural STW during the ramp topped out at
    // 30-35%, always under 45%), so heap ran unrestricted straight into the external
    // hard-ceiling backstop instead of a governed hold -- an uncontrolled abort, not the
    // mechanism working. It DID push the tail further (p99=2998ms, max=4147ms) precisely
    // because nothing intervened early, confirming the DIRECTION (allow more pressure)
    // is right for hitting 3s+ reliably. Settled on 40% as the middle point: high enough
    // to allow more sustained pressure than 35% (which only got p99 to ~2.3-2.9s across
    // two runs), low enough to actually trigger before running away to the hard ceiling
    // the way 45% did.
    private static final long GOVERNOR_HIGH_STW_PCT = 40;        // release when rolling STW fraction >= this
    private static final long GOVERNOR_LOW_STW_PCT = 15;        // must drop below this to count as "stable low"
    private static final long GOVERNOR_ROLLING_WINDOW_MS = 4000; // sampling window for the rolling STW fraction
    private static final long GOVERNOR_RELEASE_STEP_MB = 10;    // bounded partial release, never release-all
    private static final long GOVERNOR_MIN_MS_BETWEEN_RELEASES = 8000; // hysteresis: no tight flapping
    // Real, structural fix (not another threshold guess): the STW-percentage trigger
    // above proved vulnerable to a real race, confirmed twice in a row at BOTH 40% and
    // 45% -- a fast final ramp (92->109MiB in 7s, in one real run) can outrun the
    // governor's own 4s sampling window without the STW% ever crossing the chosen
    // threshold at the moment it's checked, letting heap run straight past the governor
    // to the external hard-ceiling backstop. No STW% value fixes this, because the gap
    // is architectural: the governor was only ever watching PRESSURE, never SIZE. This
    // adds a second, independent trigger that reacts to absolute post-GC heap directly,
    // regardless of what the pressure reading says at that instant -- 100MiB is real,
    // comfortably under both the external ceilings used this session (105-115MiB) and
    // the measured real thrash onset (~124MiB), so it engages with real margin to spare
    // on either side.
    // Real ripple-effect fix (this project's -Xmx bump ask): was a bare hardcoded 100,
    // valid only for the 128MiB heap it was measured against (review 54/55). Now reads a
    // real system property the bash harness sets (STAGE B.2's JAVA_OPTS patch) whenever
    // XMX_MB is bumped above 128 -- defaults to the original 100 when the property is
    // absent (every existing/default invocation is unaffected, byte-identical behavior).
    private static final long GOVERNOR_ABS_HEAP_CEILING_MIB =
        Long.getLong("wardence.leak.governorCeilingMib", 100L);
    private static final long GOVERNOR_MIN_STABLE_LOW_MS = 8000;       // must stay calm this long before re-ramping

    // Governor mode (2026-08-29, Oracle demo-visibility investigation).
    // "active" (DEFAULT -- byte-for-byte today's behavior): both the STW%-pressure
    // trigger and the absolute post-GC-heap ceiling can release retained memory.
    // "passive": ONLY the absolute ceiling acts, as a pure OOM backstop; the
    // STW%-pressure release AND its paired recovery/re-ramp logic are both skipped.
    //
    // Why this exists: the STW% trigger was built to keep the agent ALIVE at
    // -Xmx128m by shedding retained memory whenever GC pressure rose. At -Xmx192m
    // (raised 2026-08-24 after a real OOM killed the control thread) that same
    // reflex now fights the demo -- rising STW IS the fault working, and trimming
    // 10MiB at exactly that moment caps the leak below its own requested target.
    // Confirmed live on Oracle, not theorized: a real episode with target=80 ended
    // at governor_ceiling_mb=50 after 3 trims, so only ~50MiB was ever retained.
    //
    // Deliberately NOT the default: WSL2 still runs -Xmx128m (the installer never
    // patches -Xmx), where the STW trigger is still genuinely load-bearing.
    private static final boolean GOVERNOR_PASSIVE =
        "passive".equalsIgnoreCase(System.getProperty("wardence.leak.governorMode", "active"));

    // 0 = unrestricted (worker ramps freely toward targetMb); >0 = worker paused at this
    // reduced ceiling until the watchdog's governor logic lifts it back toward targetMb.
    private static volatile long governorCeilingMb = 0;
    private static volatile long governorLastReleaseAt = 0;
    private static volatile long governorStableLowSinceMs = 0;
    private static final AtomicInteger governorReleaseEvents = new AtomicInteger(0);
    // Rolling STW sampling state, owned exclusively by the watchdog thread (no lock
    // needed -- only ever read/written from that one thread).
    private static long govPrevStwMs = -1;
    private static long govPrevStwSampledAt = 0;

    // ---- request-synced GC-pressure trigger (2026-08-22 session design,
    // Kimi+Qwen reviews 61/62) ----
    // REVISED same session, real signal pivot, after direct measurement
    // (not guessed) proved the original v1 design structurally broken:
    // v1 watched currentThreadsBusy for a live 0->1 edge, meant to catch a
    // request AS it arrived. Real, direct measurement (a stall-timing
    // diagnostic added specifically to test this) showed the watcher
    // thread itself was frozen by real STW GC pauses for 968 separate
    // stalls totaling 148.3 REAL SECONDS in one single episode (worst
    // single stall: 1.78s) -- a live edge that occurs entirely inside one
    // of those frozen windows is structurally invisible to a thread that
    // is ALSO frozen for that exact window, no matter how fine the poll
    // interval is. Real result: only 1 real trigger fired the whole
    // episode despite dozens of real checkout clicks.
    // v2 (this version): watches Tomcat's own real `GlobalRequestProcessor`
    // requestCount instead -- a monotonic, cumulative counter of completed
    // requests, not a live instantaneous flag. This is IMMUNE to the same
    // freeze problem: no matter how long the watcher was frozen, the very
    // next successful read correctly sees the counter jumped, since the
    // counter itself doesn't depend on being observed at any particular
    // instant. Real, honest tradeoff, not a free win: requestCount only
    // updates via Tomcat's `registerReply()`, which fires AFTER a
    // response is already committed -- this is exactly why v1's original
    // design (reviews 61/62) rejected it for precise in-request timing.
    // The burst now fires shortly AFTER a real request completes, not
    // during it -- landing on whatever comes next (organic traffic or the
    // user's own next click) rather than the exact request that triggered
    // detection. Accepted deliberately: going from "catches ~1 of dozens
    // of real requests" to "reliably reacts to nearly all real traffic,
    // just slightly delayed onto the next one" is a real, large net
    // improvement for the actual goal (a demo that reliably FEELS the
    // fault), even though it's no longer surgically tied to one specific
    // click.
    private static final String SYNC_REQUEST_PROCESSOR_MBEAN =
        "Tomcat:type=GlobalRequestProcessor,name=\"http-nio-80\"";
    // Real, deliberate poll interval -- kept unchanged from v1 even though
    // the precision argument that originally justified 20ms no longer
    // applies (a monotonic counter can't be missed the way a live edge
    // could, so a slower poll would work just as correctly). Left as-is to
    // keep this revision scoped to the real, measured problem (the signal
    // choice) rather than also changing a knob that was never shown to be
    // wrong -- a real candidate to relax later if reqsync's own CPU/JMX
    // call overhead ever needs trimming, not yet measured as a problem.
    private static final long SYNC_POLL_MS = 20;
    // reqsync on/off (2026-08-29, Oracle demo-visibility investigation).
    // DEFAULT true -- byte-for-byte today's behavior, WSL2 untouched.
    //
    // Set false at a raised retained target. The burst is sized
    // min(freeMiB - SYNC_BURST_MARGIN_MIB, SYNC_BURST_MAX_MIB), which is
    // SELF-ARMING under exactly the conditions a high target creates: at
    // 27MiB free it still fires a 17MiB burst INTO that 27MiB, at peak
    // pressure. That shape is what produced the real -Xmx128m OOM
    // (retained 80 + burst 40 + app 26 = 146 > 128) that killed the
    // wardence-leak-control daemon thread, which the JVM never respawns.
    //
    // It is also no longer needed at a high target: its whole purpose was
    // forcing a GC near a request back when retained was low enough that
    // G1 had headroom and Eden filled too slowly on organic traffic alone.
    private static final boolean SYNC_ENABLED =
        !"false".equalsIgnoreCase(System.getProperty("wardence.leak.reqsyncEnabled", "true"));
    // Real, live-tested (2026-08-22 session): 7000ms (the middle of both
    // reviews' agreed 5-10s range) produced only 2 real triggers across a
    // full hold in the 30MiB/40MiB runs, and the real felt-effect
    // complaint became "a lot [of clicks] still resolve quite early" --
    // consistent with real clicks landing inside the 7s cooldown of a
    // prior trigger and getting no burst at all, though this couldn't be
    // directly confirmed without per-event timestamps (fixed the same
    // session -- see maybeFireSyncBurst's own real-time stderr log line).
    // Lowered to 4000ms as a prior real test step, deliberately NOT
    // further at the time -- both reviews' agreed floor was 5000ms
    // specifically to avoid a real G1 death-spiral risk on this heap;
    // going below that reviewed floor in one step, rather than testing
    // just under it first, wasn't judged worth the extra risk for an
    // unmeasured payoff at the time.
    // Real, honest note from the same session's signal pivot (see
    // SYNC_REQUEST_PROCESSOR_MBEAN's own comment): this value was tuned
    // against v1's edge-detection mechanism, which barely fired at all (1
    // real trigger/episode) -- v2's counter-based detection notices real
    // traffic far more often (confirmed live, ~18 real triggers/episode),
    // meaning THIS debounce value is the real, live-binding constraint.
    // Lowered again to 3000ms, still below both reviews' reviewed 5000ms
    // floor -- a further deliberate step under v2 detection to test
    // whether more frequent triggers catch more real checkout clicks,
    // at the same acknowledged G1 death-spiral risk.
    // **LOCKED, real live-tested result (2026-08-23 session), the
    // demo-visibility arc's actual close: at 3000ms/40MiB, EVERY real
    // manual checkout click during a live hold felt a delay (>1s), not
    // just most of them -- the first time in the whole arc this
    // happened. 2 of those real clicks needed a retry before completing
    // (see SYNC_BURST_MAX_MIB's own comment for why: some real STALL
    // durations now brush against orders' 5s downstream timeout to
    // shipping). Real, deliberate design decision, not an accepted
    // side effect: this is the new intended behavior, not a regression
    // from the original "always completes, never errors" spec -- a
    // real failure a user has to retry reads as MORE dramatic for a
    // demo, not worse, and it doesn't touch diagnosis/scoring (both
    // are pure Prometheus heap-metric reads, independent of checkout
    // outcome) or risk a crash-loop-shaped restart (a Future.get
    // timeout throws cleanly on the orders side, confirmed via the
    // earlier cascading-dependency-failure source investigation --
    // neither shipping nor orders restarts). No further tuning planned
    // unless this specific tradeoff is ever revisited.
    private static final long SYNC_DEBOUNCE_MS = 3000;
    // NOT yet empirically tuned -- real headroom kept below the dynamic
    // free-heap estimate so a stale (racy) read of totalMemory()/
    // freeMemory() at burst time doesn't itself trigger a real OOM. The
    // burst allocation is wrapped in a try/catch regardless (real OOM
    // here is treated as "the estimate was stale, skip this trigger", not
    // fatal), but this margin is meant to make that catch a rare
    // safety net, not the normal path.
    private static final long SYNC_BURST_MARGIN_MIB = 10;
    // Real, deliberate bound. Without this, the burst size (freeMib -
    // margin) would allocate essentially ALL remaining headroom on every
    // single trigger: fights the governor's own graduated
    // GOVERNOR_RELEASE_STEP_MB=10MiB steps instead of adding a bounded
    // pulse on top of them, and risks racing the worker thread's own
    // concurrent allocation straight into a real OOM.
    // Real, live-tested tuning pass (2026-08-22 session), each step run as
    // a full real live episode with real manual checkout clicks, not
    // simulated:
    //   20MiB: 5 real triggers, zero checkout failures, felt effect real
    //     but inconsistent (>half of ~10 manual clicks unaffected, 2
    //     genuinely hung 1+s, a few 0.3-0.5s).
    //   30MiB: only 2 real triggers this run, zero checkout failures, felt
    //     effect MORE consistent than 20MiB despite fewer triggers -- every
    //     single click during the active hold felt genuinely slower
    //     (mostly ~0.5s, rarely 1+s). Real, meaningful improvement in
    //     reliability of the felt effect, not just severity.
    // Real ceiling reasoning, not a guess: the governor already runs the
    // retained leak near ~100-111MiB during a hold (confirmed live), and
    // this project's own earlier governed-leak tuning recorded a real 97%
    // STW-pause fraction at just 86MiB heap_used and a real ~124MiB thrash
    // onset -- severe GC pressure starts well below the 128MiB Xmx
    // ceiling, not at it. A burst fires ON TOP of the already-elevated
    // governed-hold baseline, so raising this risks pushing the COMBINED
    // live-set into that pressure zone (a full/mixed GC instead of a quick
    // minor one) well before the min(freeMib-margin, cap) sizing formula's
    // own real-time headroom check would ever risk an actual OOM.
    //   40MiB: same 2 real triggers as 30MiB (a real, honest caveat: with
    //     only a cumulative counter and no per-event timestamps at the
    //     time, this could mean "burst size doesn't affect trigger
    //     frequency" OR just fewer real requests landed in this window --
    //     genuinely couldn't distinguish the two). Felt effect LESS
    //     consistent than 30MiB despite occasional higher severity (a
    //     couple 2s hangs vs. 30MiB's rare 1s) -- "a lot still resolve
    //     quite early," no clear improvement over 30MiB at the time.
    // **Reverted to 30MiB at the time** -- the cleanest real result of the
    // three, and pushing the cap further wasn't judged the right lever: it
    // mainly controls the SEVERITY of one trigger, not how many real
    // clicks get hit at all (that's SYNC_DEBOUNCE_MS's job). **Real,
    // honest caveat on that whole 20/30/40 comparison, flagged the same
    // session:** it ran entirely under v1's edge-detection mechanism
    // (~2 real triggers/episode) -- explicitly stale now that v2's
    // counter-based detection fires far more often (~18/episode), which
    // could change how burst size interacts with trigger frequency.
    // Raised back to 40MiB to re-test under v2, alongside the lowered
    // SYNC_DEBOUNCE_MS.
    // **LOCKED, real live-tested result (2026-08-23 session): 40MiB +
    // 3000ms debounce is what closed the demo-visibility arc for real
    // -- see SYNC_DEBOUNCE_MS's own comment for the full real result
    // and the locked design decision. The earlier stale 20/30/40MiB
    // comparison above (under v1 detection) is superseded by this real
    // result under v2, not still the standing verdict.
    private static final long SYNC_BURST_MAX_MIB = 40;
    // Real, deliberate design per Qwen's review-62 fix: sized against
    // REAL-TIME free heap, not a fixed constant -- this is what makes the
    // burst proportional to actual current pressure rather than
    // potentially being silently absorbed by whatever headroom the
    // governor happens to have left at that instant.
    private static volatile long syncLastTriggerAtMs = 0;
    private static final AtomicInteger syncTriggerCount = new AtomicInteger(0);
    private static final AtomicInteger syncSkippedNoHeadroomCount = new AtomicInteger(0);
    private static final AtomicBoolean syncMbeanUnavailable = new AtomicBoolean(false);

    // Real diagnostic instrumentation added 2026-08-22, to directly test
    // (not just guess) whether STW GC pauses freezing this SAME polling
    // thread explain the real observed low busy-edge detection rate (2
    // real triggers out of ~35 real checkout attempts in the same
    // session's live test) -- a stop-the-world pause freezes every Java
    // thread including this one, so a real request that fully arrives and
    // completes inside such a pause is structurally invisible to a polling
    // loop that is ALSO frozen for that exact window. If this thread's own
    // measured cycle time regularly blows past its expected ~SYNC_POLL_MS,
    // that's direct, real evidence of the freeze happening, not inference.
    private static final long SYNC_STALL_THRESHOLD_MS = 50;
    private static final AtomicInteger syncStallCount = new AtomicInteger(0);
    private static final AtomicLong syncStallMsTotal = new AtomicLong(0);
    private static volatile long syncMaxStallMs = 0;

    private static volatile int targetMb = 0;
    private static volatile long allocatedBytes = 0L;
    private static volatile String currentState = "READY";
    private static volatile int lastFailedTargetMb = -1;
    private static final AtomicBoolean ABORT_ALLOCATION = new AtomicBoolean(false);

    // ---- native (off-heap) memory-pressure mode (2026-08-30) ----
    // DELIBERATELY SEPARATE from the heap leak above and from every GC/governor
    // mechanism in this file. A `NATIVE <mb> [ttl=<s>]` control command drives real,
    // resident, off-heap memory via sun.misc.Unsafe.allocateMemory -- this grows the
    // CONTAINER's RSS toward its cgroup memory limit WITHOUT touching the Java heap, so
    // the garbage collector is not involved at all. Purpose: test whether cgroup memory
    // pressure (kernel reclaiming the JVM's own file-backed code pages -> major page
    // faults -> request stutter) produces a felt storefront slowdown where a retained
    // heap leak provably cannot (reviews 67-69 + the paced-climb test, 2026-08-30).
    // `NATIVE 0` or `RELEASE` frees all of it. Every chunk is written page-by-page on
    // allocation so it is genuinely resident, not just reserved address space.
    // Byte-identical no-op for every existing ALLOCATE/heap-leak episode: nativeTargetBytes
    // starts 0 and nativeLoop() does nothing until a NATIVE command arrives.
    private static final long NATIVE_CHUNK_BYTES = 4L * 1024 * 1024; // 4 MiB
    private static volatile long nativeTargetBytes = 0L;
    private static final java.util.List<Long> NATIVE_CHUNKS = new java.util.ArrayList<Long>();
    private static final Object NATIVE_LOCK = new Object();
    private static volatile long nativeAllocatedBytes = 0L;
    private static volatile boolean nativeAllocFailed = false;
    // Resolved reflectively (no direct sun.misc.Unsafe type reference -> compiles clean
    // on every JDK8 build, including ones whose javac symbol file hides sun.* classes).
    private static Object UNSAFE = null;
    private static java.lang.reflect.Method U_ALLOC = null; // allocateMemory(long) -> long
    private static java.lang.reflect.Method U_FREE = null;  // freeMemory(long)
    private static java.lang.reflect.Method U_SET = null;   // setMemory(long,long,byte)

    // ---- Reference-processing-inflation mode (2026-08-30, Gemini idea #1) ----
    // DELIBERATELY SEPARATE from every other mechanism in this file. A `REFLEAK <count>
    // [ttl=<s>]` command retains <count> java.lang.ref.SoftReference wrappers, each
    // holding a tiny referent that is otherwise unreachable. Purpose: test whether a
    // real SoftReference-cache leak inflates the GC's mandatory Reference-Processing
    // phase (O(N) in discovered refs) enough to add felt STW latency to checkout, where
    // an inert retained byte[] leak provably cannot (every prior test 2026-08-2x..30).
    // Both the wrappers (~40B) and the live-because-not-yet-cleared referents (~24B)
    // grow the heap, so the heap_rise_kb diagnosis signal survives; fault identity stays
    // "memory leak". `REFLEAK 0` / `RELEASE` drops them. Byte-identical no-op until a
    // REFLEAK command arrives (refLeakTarget starts 0).
    private static final int REFLEAK_REFERENT_BYTES = 16;
    private static final int REFLEAK_BATCH = 4000; // per ~100ms convergence tick
    private static volatile long refLeakTarget = 0L;
    private static volatile long refLeakCount = 0L;
    private static final java.util.ArrayList<java.lang.ref.SoftReference<byte[]>> REFLEAK_LIST =
        new java.util.ArrayList<java.lang.ref.SoftReference<byte[]>>();
    private static final Object REFLEAK_LOCK = new Object();

    // ---- JIT CodeCache-exhaustion mode (2026-08-30, Gemini idea #3) ----
    // DELIBERATELY SEPARATE from every other mechanism. A `CODELEAK <count> [ttl=<s>]`
    // command generates <count> distinct trivial classes (hand-built bytecode, no
    // library) each held alive via its own ClassLoader, and warms each one's method so
    // the JIT compiles it into the non-heap CodeCache. Purpose: test whether filling
    // CodeCache (compiler shuts down -> hot app paths fall back to INTERPRETED execution,
    // ~10x slower, no GC involvement, no crash) produces a felt, stable checkout
    // slowdown where every heap/GC mechanism provably cannot. This is the canonical
    // real-world Java "memory leak" (ClassLoader / dynamic-proxy / bytecode leak).
    // Diagnosis signal shifts to jvm nonheap "Code Cache" usage (a real mechanism
    // change -> continuity note). `CODELEAK 0` / `RELEASE` drops the loaders; the
    // CodeCache sweeper then reclaims the flushed methods and the compiler re-enables.
    // Needs a shrunk -XX:ReservedCodeCacheSize on the JVM to fill in a demo window.
    // Byte-identical no-op until a CODELEAK command arrives (codeLeakTarget starts 0).
    private static final int CODELEAK_HOT_INVOCATIONS = 2500; // enough to force C1 compilation
    private static final int CODELEAK_BATCH = 20;             // classes generated+warmed per ~100ms tick
    private static volatile long codeLeakTarget = 0L;
    private static volatile long codeLeakCount = 0L;
    private static volatile boolean codeLeakFailed = false;
    private static final java.util.ArrayList<Object[]> CODELEAK_HOLD = new java.util.ArrayList<Object[]>();
    private static final Object CODELEAK_LOCK = new Object();
    private static java.lang.management.MemoryPoolMXBean CODE_CACHE_POOL = null;

    private static final class GenCL extends ClassLoader {
        GenCL() { super(GenCL.class.getClassLoader()); }
        Class<?> define(String binName, byte[] b) { return defineClass(binName, b, 0, b.length); }
    }

    // ---- CHURN mode: medium-lived promoted-then-garbage stream (2026-09-01) ----
    // DELIBERATELY SEPARATE from every other mechanism in this file. A `CHURN <mb>
    // <hold_ms> [linked|dense] [static=<mb>] [ttl=<s>]` command maintains a ring buffer of ~<mb> MiB
    // where each 256KiB chunk is held for <hold_ms> then dropped and replaced. Every
    // prior attempt attacked condition #1 (fill/hold old gen with PERMANENT retained
    // junk). This attacks condition #2: a continuous stream of objects that live long
    // enough to be PROMOTED into old gen (hold_ms >= ~2s comfortably survives a young GC
    // under load), then become garbage IN old gen when they fall off the ring -- forcing
    // repeated old-gen collections for as long as the stream runs. Bounded ring => can
    // never OOM => no cliff by construction (the "no bounded middle" problem from the
    // paced-climb test was about a GROWING retained set; this is not that). `static=<mb>`
    // (optional) additionally drives the existing retained-leak worker to pre-fill old
    // gen so the churn garbage triggers a collection with far less headroom. `CHURN 0` /
    // RELEASE drains it. Byte-identical no-op until a CHURN command arrives.
    private static final int CHURN_CHUNK_BYTES = 256 * 1024; // 256 KiB, sub-humongous
    // `linked` variant (2026-09-01, user's own idea #1): each churned entry, instead of
    // an inert byte[], is a small node holding a byte[] payload PLUS refs to a few OTHER
    // recent ring entries -- so a young GC must chase pointers and maintain cross-region
    // remembered-set/card-table bookkeeping every collection, not just copy bytes. Refs
    // point only at entries within a bounded recent window (the "hub" fix from the
    // 2026-08 object-graph work: without a window, the oldest few nodes get referenced by
    // nearly every later node). With FIFO head-eviction, an evicted node stays reachable
    // via forward refs from newer nodes until the window slides past it ~CHURN_REF_WINDOW
    // steps later -- so garbage is generated as LINKED CLUSTERS that fall unreachable
    // together, more tracing/reclaim work for the GC than scattered arrays. Flat (default)
    // mode is a ChurnNode with an empty refs array -- functionally the original design.
    private static final int CHURN_REFS_PER_NODE = 6;
    private static final int CHURN_REF_WINDOW = 40;
    private static final java.util.Random CHURN_RANDOM = new java.util.Random();
    private static final Object[] CHURN_EMPTY_REFS = new Object[0];
    // `dense` variant (2026-09-01, user's #1+#2): GC mark/compact cost is O(live object
    // COUNT and reference count), not bytes -- a 48MiB flat byte[] ring is ~190 objects
    // and marks in microseconds. In dense mode each ring entry is a CLUSTER: a deep
    // singly-linked chain of ~CHURN_DENSE_NODES tiny nodes (each an int[8] + a next ref,
    // ~72B). Touching a cluster forces the GC to chase the chain node-by-node (cache-miss
    // per hop, no prefetch/parallelism) and mark every node + every int[] individually.
    // A 48MiB dense ring is ~1.2M objects. The chain promotes into old gen incrementally
    // over successive young GCs (each one copies the frontier now reachable from old ->
    // continuous young-GC cost, which is what checkout requests actually hit). Eviction
    // drops the head ref -> the whole chain dies as one unreachable cluster. NO
    // cross-cluster refs -> no transitive-leak risk (unlike `linked`).
    private static final int CHURN_DENSE_NODE_INTS = 8;
    private static final int CHURN_DENSE_CLUSTER_BYTES = 256 * 1024;
    private static final int CHURN_DENSE_NODE_BYTES = 72; // ~ DenseNode(24) + int[8](48), compressed oops
    private static final int CHURN_DENSE_NODES = CHURN_DENSE_CLUSTER_BYTES / CHURN_DENSE_NODE_BYTES;
    private static final class DenseNode {
        final int[] pad;
        DenseNode next;
        DenseNode(int[] p) { this.pad = p; }
    }
    private static final class ChurnNode {
        final long createdAtMs;
        final byte[] payload;  // flat/linked: the 256KiB array. dense: null.
        Object[] refs;         // linked: refs to recent nodes, NULLED on eviction so an
                               // evicted node becomes a leaf (else the backward-reference
                               // web keeps every evicted node transitively reachable from
                               // the live ring and the "bounded" ring leaks the whole
                               // history -- crash, 2026-09-01). flat/dense: empty.
        final Object cluster;  // dense: the DenseNode chain head. flat/linked: null.
        final int approxBytes;
        ChurnNode(long t, byte[] p, Object[] r, Object c, int b) {
            this.createdAtMs = t; this.payload = p; this.refs = r; this.cluster = c; this.approxBytes = b;
        }
    }
    private static volatile boolean churnLinked = false;
    private static volatile boolean churnDense = false;
    private static volatile long churnTargetBytes = 0L;
    private static volatile long churnLiveBytes = 0L;
    private static volatile long churnHoldMs = 2500L;
    // Head = oldest.
    private static final java.util.ArrayDeque<ChurnNode> CHURN_RING = new java.util.ArrayDeque<ChurnNode>();
    private static final Object CHURN_LOCK = new Object();
    private static final AtomicLong churnEvictions = new AtomicLong(0L);

    // ---- GRAPH mode: dense, constantly-rewritten reference graph in old gen (2026-09-01,
    //      user's #A+#B fused) ----
    // GC mark cost is O(references/EDGES traversed), and young-GC cost is O(dirty cards
    // rescanned). This holds ONE big retained Object[] "backbone" (-> old gen) whose slots
    // each point to a GraphNode carrying `graphEdges` references to random OTHER backbone
    // slots -- a dense graph of ~graphSlots nodes and graphSlots*graphEdges edges. A
    // background thread overwrites random backbone slots at graphWritesPerSec: each store
    // (a) dirties that slot's card -> every young GC must rescan it and re-examine every
    // node on it (all its edges), and (b) makes the previous slot occupant old-gen
    // garbage. So EVERY young GC (frequent, on the request path) pays O(all edges), and
    // every Full GC's mark phase traverses the whole edge set. `GRAPH 0` / RELEASE nulls
    // the backbone. Byte-identical no-op until a GRAPH command arrives.
    private static final int GRAPH_NODE_PAD_INTS = 4; // int[4] ~= 32B pad per node
    private static final class GraphNode {
        final int[] pad;
        Object[] edges; // NULLED when this node is evicted from the backbone (see graphLoop)
        GraphNode(int[] p, Object[] e) { this.pad = p; this.edges = e; }
    }
    private static volatile Object[] graphBackbone = null;
    private static volatile int graphSlots = 0;
    private static volatile int graphWritesPerSec = 0;
    private static volatile int graphEdges = 0;
    private static final java.util.Random GRAPH_WRITE_RANDOM = new java.util.Random();
    private static final AtomicLong graphWritesTotal = new AtomicLong(0L);

    // ---- real GC observation state (review 57) ----
    // Kimi and Qwen DISAGREED on whether summed GarbageCollectorMXBean.getCollectionTime()
    // is a valid "application stopped time" signal: Kimi said the beans are correct and
    // only the bash math was broken; Qwen said summing across G1's Young and Old beans can
    // include concurrent (non-STW) work, which is why fractions above 100% were even
    // possible. Rather than pick a side, this records BOTH, separately, so the next real
    // run settles it with evidence instead of argument:
    //   gc_time_ms      -- the old summed-bean number, kept for continuity/comparison
    //   stw_pause_ms    -- accumulated ONLY from real GC notifications whose cause is not
    //                      "No GC" (G1's concurrent-cycle-end notifications report cause
    //                      "No GC" and carry concurrent, non-stop-the-world duration --
    //                      exactly the contamination Qwen flagged)
    // If the two diverge meaningfully in a real run, Qwen was right and stw_pause_ms is
    // the signal to trust. If they track each other closely, Kimi was right.
    // lastPostGcHeapBytes is a plain volatile: a single assignment of a long IS atomic
    // when volatile, and last-writer-wins is exactly the semantics wanted here.
    // The two counters below are AtomicLong, NOT volatile long: `+=` and `++` are
    // read-modify-write, which volatile does not make atomic, and notifications can in
    // principle be delivered concurrently from G1's two collector beans.
    private static volatile long lastPostGcHeapBytes = -1L;
    private static final AtomicLong stwPauseMsTotal = new AtomicLong(0L);
    private static final AtomicLong gcNotificationCount = new AtomicLong(0L);

    // ---- per-thread liveness, all volatile/atomic, read by the watchdog and by
    // writeStatus() -- never touched by more than one thread's own "owner" logic ----
    private static final AtomicLong heartbeatAt = new AtomicLong(System.currentTimeMillis());
    private static final AtomicLong controlLastRunAt = new AtomicLong(System.currentTimeMillis());
    private static final AtomicLong workerLastProgressAt = new AtomicLong(System.currentTimeMillis());
    private static final AtomicBoolean ioStuck = new AtomicBoolean(false);
    private static final AtomicBoolean workerStuck = new AtomicBoolean(false);
    private static final AtomicBoolean controlStuck = new AtomicBoolean(false);
    private static final AtomicInteger watchdogReleases = new AtomicInteger(0);
    private static volatile String lastError = "";
    private static volatile long cmdMtimeAtRead = 0L;
    private static volatile long cmdTtlS = 60L;
    // mtime of the last RELEASE command actually acted on. The command file is
    // NOT deleted after a successful read (only when stale, see controlLoop) --
    // it is a persistent "current command", and every other handler
    // de-duplicates by comparing the file mtime it was invoked with. The RELEASE
    // branch did neither, so a RELEASE sitting in the file re-fired
    // requestRelease() once per second forever: state -> RELEASING, worker ->
    // IDLE, next tick -> RELEASING again. Observed live 2026-09-02, stuck in
    // RELEASING for 16 minutes with allocated_mb=0 and graph_slots=0 (everything
    // genuinely freed; only the reported state was wrong). This field restores
    // the same mtime de-duplication the other commands already use.
    private static volatile long lastReleaseCmdMtime = -1L;

    private static final File CTL_DIR = new File("/agent-ctl");
    private static final File CMD_FILE = new File(CTL_DIR, "cmd");
    private static final File STATUS_FILE = new File(CTL_DIR, "status");
    private static final File STATUS_TMP = new File(CTL_DIR, "status.tmp");
    private static final File BEAT_FILE = new File(CTL_DIR, "beat");
    private static final File BEAT_TMP = new File(CTL_DIR, "beat.tmp");
    private static final long AGENT_START_MS = System.currentTimeMillis();

    // Bounded I/O executor -- Qwen's point B: every file read/write goes through this,
    // never raw blocking I/O directly on the control thread. 2 threads: one for the
    // in-flight op, headroom for one more if a prior op is still stuck when the next
    // cycle starts (never queue-starved on a single stuck thread).
    private static final ExecutorService IO_POOL = Executors.newFixedThreadPool(2, new java.util.concurrent.ThreadFactory() {
        public Thread newThread(Runnable r) {
            Thread t = new Thread(r, "wardence-leak-agent-io");
            t.setDaemon(true);
            return t;
        }
    });

    private static long lastIoReadMs = 0L;
    private static long lastIoWriteMs = 0L;

    // Registers a real GC notification listener (review 57). Fully wrapped in try/catch
    // and never allowed to fail the agent: if com.sun.management isn't available on this
    // specific JDK8 build, post_gc_heap_mib simply stays -1 and stw_pause_ms stays 0,
    // rather than breaking the agent or the app it's attached to.
    private static void registerGcListener() {
        try {
            for (GarbageCollectorMXBean bean : ManagementFactory.getGarbageCollectorMXBeans()) {
                if (!(bean instanceof NotificationEmitter)) continue;
                ((NotificationEmitter) bean).addNotificationListener(new NotificationListener() {
                    public void handleNotification(Notification notification, Object handback) {
                        try {
                            if (!GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION
                                    .equals(notification.getType())) {
                                return;
                            }
                            GarbageCollectionNotificationInfo info =
                                GarbageCollectionNotificationInfo.from((CompositeData) notification.getUserData());
                            gcNotificationCount.incrementAndGet();

                            // Real post-GC heap: sum the HEAP pools this collector manages,
                            // deliberately excluding non-heap pools (Metaspace/Code Cache/
                            // Compressed Class Space) which some JVMs include here and which
                            // would silently inflate the number.
                            Map<String, MemoryUsage> after = info.getGcInfo().getMemoryUsageAfterGc();
                            if (after != null) {
                                long used = 0L;
                                for (Map.Entry<String, MemoryUsage> e : after.entrySet()) {
                                    String pool = e.getKey();
                                    if (pool == null) continue;
                                    if (pool.contains("Metaspace") || pool.contains("Code")
                                            || pool.contains("Compressed Class")) {
                                        continue;
                                    }
                                    used += e.getValue().getUsed();
                                }
                                lastPostGcHeapBytes = used;
                            }

                            // Real STW pause accumulation, filtered per Qwen's concern:
                            // G1's concurrent-cycle-end notifications carry cause "No GC"
                            // and a duration covering concurrent (non-stopping) work.
                            // Counting those is exactly what makes a "stopped time
                            // fraction" able to exceed 100%.
                            String cause = info.getGcCause();
                            if (cause == null || !"No GC".equalsIgnoreCase(cause)) {
                                stwPauseMsTotal.addAndGet(info.getGcInfo().getDuration());
                            }
                        } catch (Throwable ignored) {
                            // never let a notification callback destabilize the app
                        }
                    }
                }, null, null);
            }
            System.err.println("[wardence-leak-agent] GC notification listener registered");
        } catch (Throwable t) {
            System.err.println("[wardence-leak-agent] GC listener registration failed (non-fatal, "
                + "post_gc_heap_mib/stw_pause_ms will be unavailable): " + t);
        }
    }

    // Resolve sun.misc.Unsafe once, reflectively (no static import to keep the compile
    // clean on any JDK8 build). Fully non-fatal: if it can't be resolved, UNSAFE stays
    // null, nativeLoop() becomes a no-op, and native_alloc_failed reports it -- the heap
    // leak and everything else is untouched.
    private static void initUnsafe() {
        try {
            Class<?> uc = Class.forName("sun.misc.Unsafe");
            java.lang.reflect.Field f = uc.getDeclaredField("theUnsafe");
            f.setAccessible(true);
            UNSAFE = f.get(null);
            U_ALLOC = uc.getMethod("allocateMemory", long.class);
            U_FREE = uc.getMethod("freeMemory", long.class);
            U_SET = uc.getMethod("setMemory", long.class, long.class, byte.class);
            System.err.println("[wardence-leak-agent] sun.misc.Unsafe resolved via reflection -- native-memory mode available");
        } catch (Throwable t) {
            UNSAFE = null; U_ALLOC = null; U_FREE = null; U_SET = null;
            System.err.println("[wardence-leak-agent] sun.misc.Unsafe unavailable (non-fatal, NATIVE command "
                + "will be a no-op): " + t);
        }
    }

    // ================= Thread: native (off-heap) memory pressure =================
    // Converges real resident off-heap memory toward nativeTargetBytes, in
    // NATIVE_CHUNK_BYTES steps, every ~200ms. Grows via Unsafe.allocateMemory +
    // setMemory (page-touch so the kernel backs it with real pages, i.e. it shows up in
    // container RSS immediately). Shrinks via freeMemory. All list mutation under
    // NATIVE_LOCK. A native allocation failure (native OOM) sets a flag and stops
    // growing rather than throwing -- freeing any chunk clears the flag so it can retry.
    private static void nativeLoop() {
        while (true) {
            try {
                long target = nativeTargetBytes;
                boolean ready = (UNSAFE != null && U_ALLOC != null && U_FREE != null && U_SET != null);
                synchronized (NATIVE_LOCK) {
                    while (ready && !nativeAllocFailed
                            && nativeAllocatedBytes + NATIVE_CHUNK_BYTES <= target) {
                        try {
                            long addr = (Long) U_ALLOC.invoke(UNSAFE, NATIVE_CHUNK_BYTES);
                            U_SET.invoke(UNSAFE, addr, NATIVE_CHUNK_BYTES, (byte) 0);
                            NATIVE_CHUNKS.add(addr);
                            nativeAllocatedBytes += NATIVE_CHUNK_BYTES;
                        } catch (Throwable oom) {
                            nativeAllocFailed = true;
                            lastError = "native alloc failed at "
                                + (nativeAllocatedBytes / (1024 * 1024)) + "MiB: " + oom;
                            break;
                        }
                    }
                    while (ready && !NATIVE_CHUNKS.isEmpty()
                            && nativeAllocatedBytes - NATIVE_CHUNK_BYTES >= target) {
                        long addr = NATIVE_CHUNKS.remove(NATIVE_CHUNKS.size() - 1);
                        try { U_FREE.invoke(UNSAFE, addr); } catch (Throwable ignored) {}
                        nativeAllocatedBytes -= NATIVE_CHUNK_BYTES;
                        nativeAllocFailed = false;
                    }
                }
                Thread.sleep(200);
            } catch (Throwable t) {
                lastError = "native: " + t;
            }
        }
    }

    private static void handleNative(String line, long cmdMtime) {
        String[] parts = line.split("\\s+");
        if (parts.length < 2) return;
        long mb;
        long ttlS = 60;
        try {
            mb = Long.parseLong(parts[1]);
            for (int i = 2; i < parts.length; i++) {
                if (parts[i].startsWith("ttl=")) ttlS = Long.parseLong(parts[i].substring(4));
            }
        } catch (NumberFormatException e) {
            return;
        }
        cmdMtimeAtRead = cmdMtime;
        cmdTtlS = ttlS;
        nativeTargetBytes = Math.max(0L, mb) * 1024L * 1024L;
    }

    // ================= Thread: reference-processing-inflation leak =================
    // Converges REFLEAK_LIST size toward refLeakTarget in REFLEAK_BATCH steps every
    // ~100ms. Each entry is a SoftReference wrapping a fresh 16-byte referent held ONLY
    // by that soft ref -> the GC must "discover" and policy-evaluate it every marking
    // cycle (the O(N) Reference-Processing cost being tested). Under real memory
    // pressure G1 will clear them all at once (ref.get() -> null); the wrappers stay,
    // the referents are reclaimed -- that oscillation is itself a real datapoint, not a
    // bug. Never throws OOM up: an allocation failure just stops growth for this tick.
    private static void refLeakLoop() {
        while (true) {
            try {
                long target = refLeakTarget;
                synchronized (REFLEAK_LOCK) {
                    int added = 0;
                    while (refLeakCount < target && added < REFLEAK_BATCH) {
                        try {
                            REFLEAK_LIST.add(new java.lang.ref.SoftReference<byte[]>(new byte[REFLEAK_REFERENT_BYTES]));
                            refLeakCount++;
                            added++;
                        } catch (OutOfMemoryError oom) {
                            lastError = "refleak alloc failed at count=" + refLeakCount + ": " + oom;
                            break;
                        }
                    }
                    int removed = 0;
                    while (refLeakCount > target && !REFLEAK_LIST.isEmpty() && removed < REFLEAK_BATCH) {
                        REFLEAK_LIST.remove(REFLEAK_LIST.size() - 1);
                        refLeakCount--;
                        removed++;
                    }
                    if (target == 0 && REFLEAK_LIST.isEmpty()) {
                        REFLEAK_LIST.trimToSize(); // hand the backing array back after a full release
                    }
                }
                Thread.sleep(100);
            } catch (Throwable t) {
                lastError = "refleak: " + t;
            }
        }
    }

    private static void handleRefLeak(String line, long cmdMtime) {
        String[] parts = line.split("\\s+");
        if (parts.length < 2) return;
        long count;
        long ttlS = 60;
        try {
            count = Long.parseLong(parts[1]);
            for (int i = 2; i < parts.length; i++) {
                if (parts[i].startsWith("ttl=")) ttlS = Long.parseLong(parts[i].substring(4));
            }
        } catch (NumberFormatException e) {
            return;
        }
        cmdMtimeAtRead = cmdMtime;
        cmdTtlS = ttlS;
        refLeakTarget = Math.max(0L, count);
    }

    // ================= Thread: CodeCache-exhaustion leak =================
    private static void initCodeCachePool() {
        try {
            for (java.lang.management.MemoryPoolMXBean p : ManagementFactory.getMemoryPoolMXBeans()) {
                String n = p.getName();
                if (n != null && n.toLowerCase().contains("code")) { CODE_CACHE_POOL = p; break; }
            }
        } catch (Throwable ignored) { CODE_CACHE_POOL = null; }
    }

    // Minimal Java-8 (major 52) class file for:  public final class <name> { public
    // static int f(int x){ return x*31 + x*7 - (x>>1) ^ x; } }  -- straight-line, no
    // branches, so no StackMapTable needed. 7-entry constant pool.
    private static byte[] genClassBytes(String internalName) {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        try {
            java.io.DataOutputStream d = new java.io.DataOutputStream(bos);
            d.writeInt(0xCAFEBABE);
            d.writeShort(0);   // minor
            d.writeShort(50);  // major = Java 6 -- deliberately <=50 so the OLD (inference,
                               // no-StackMapTable) verifier is used; a hand-built straight-
                               // line method verifies with zero stackmap hassle. Java 8
                               // loads v50 classes fine.
            d.writeShort(8);   // constant_pool_count = 7 entries + 1
            d.writeByte(7); d.writeShort(2);                 // #1 Class -> #2
            d.writeByte(1); d.writeUTF(internalName);        // #2 Utf8 this-class name
            d.writeByte(7); d.writeShort(4);                 // #3 Class -> #4
            d.writeByte(1); d.writeUTF("java/lang/Object");  // #4 Utf8 super
            d.writeByte(1); d.writeUTF("f");                 // #5 Utf8 method name
            d.writeByte(1); d.writeUTF("(I)I");              // #6 Utf8 descriptor
            d.writeByte(1); d.writeUTF("Code");              // #7 Utf8 "Code"
            d.writeShort(0x0031); // ACC_PUBLIC | ACC_FINAL | ACC_SUPER
            d.writeShort(1);      // this_class -> #1
            d.writeShort(3);      // super_class -> #3
            d.writeShort(0);      // interfaces
            d.writeShort(0);      // fields
            d.writeShort(1);      // methods
            d.writeShort(0x0009); // ACC_PUBLIC | ACC_STATIC
            d.writeShort(5);      // name -> #5
            d.writeShort(6);      // descriptor -> #6
            d.writeShort(1);      // method attributes
            d.writeShort(7);      // attribute name -> #7 "Code"
            byte[] code = new byte[] {
                0x1a,             // iload_0
                0x10, 0x1f,       // bipush 31
                0x68,             // imul           -> x*31
                0x1a,             // iload_0
                0x10, 0x07,       // bipush 7
                0x68,             // imul           -> x*7
                0x60,             // iadd           -> x*31 + x*7
                0x1a,             // iload_0
                0x04,             // iconst_1
                0x7a,             // ishr           -> x>>1
                0x64,             // isub           -> (...) - (x>>1)
                0x1a,             // iload_0
                (byte) 0x82,      // ixor           -> (...) ^ x
                (byte) 0xac       // ireturn
            };
            d.writeInt(2 + 2 + 4 + code.length + 2 + 2); // Code attribute length
            d.writeShort(3);      // max_stack
            d.writeShort(1);      // max_locals
            d.writeInt(code.length);
            d.write(code);
            d.writeShort(0);      // exception_table_length
            d.writeShort(0);      // code attributes_count
            d.writeShort(0);      // CLASS-level attributes_count (was missing -> "Truncated class file")
            d.flush();
        } catch (java.io.IOException e) {
            return null;
        }
        return bos.toByteArray();
    }

    private static void codeLeakLoop() {
        while (true) {
            try {
                long target = codeLeakTarget;
                if (codeLeakCount < target && !codeLeakFailed) {
                    int made = 0;
                    while (codeLeakCount < target && made < CODELEAK_BATCH) {
                        try {
                            String simple = "W" + codeLeakCount;
                            byte[] b = genClassBytes("wardence/gen/" + simple);
                            if (b == null) { codeLeakFailed = true; lastError = "codeleak: genClassBytes null"; break; }
                            GenCL cl = new GenCL();
                            Class<?> c = cl.define("wardence.gen." + simple, b);
                            java.lang.reflect.Method m = c.getMethod("f", int.class);
                            int acc = 0;
                            for (int i = 0; i < CODELEAK_HOT_INVOCATIONS; i++) {
                                acc += ((Integer) m.invoke(null, Integer.valueOf(i))).intValue();
                            }
                            synchronized (CODELEAK_LOCK) {
                                CODELEAK_HOLD.add(new Object[] { cl, c, m, Integer.valueOf(acc) });
                            }
                            codeLeakCount++;
                            made++;
                        } catch (Throwable t) {
                            codeLeakFailed = true;
                            lastError = "codeleak failed at count=" + codeLeakCount + ": " + t;
                            System.err.println("[wardence-leak-agent] " + lastError);
                            t.printStackTrace();
                            break;
                        }
                    }
                } else if (target == 0 && codeLeakCount > 0) {
                    synchronized (CODELEAK_LOCK) { CODELEAK_HOLD.clear(); }
                    codeLeakCount = 0;
                    codeLeakFailed = false;
                    System.gc(); // nudge classloader unloading -> CodeCache sweep -> compiler re-enables
                }
                Thread.sleep(100);
            } catch (Throwable t) {
                lastError = "codeleak: " + t;
            }
        }
    }

    private static void handleCodeLeak(String line, long cmdMtime) {
        String[] parts = line.split("\\s+");
        if (parts.length < 2) return;
        long count;
        long ttlS = 60;
        try {
            count = Long.parseLong(parts[1]);
            for (int i = 2; i < parts.length; i++) {
                if (parts[i].startsWith("ttl=")) ttlS = Long.parseLong(parts[i].substring(4));
            }
        } catch (NumberFormatException e) {
            return;
        }
        cmdMtimeAtRead = cmdMtime;
        cmdTtlS = ttlS;
        codeLeakTarget = Math.max(0L, count);
    }

    // ================= Thread: churn (medium-lived promoted-then-garbage stream) =======
    // Every ~150ms, under CHURN_LOCK: (1) evict from the head every chunk older than
    // churnHoldMs (dropping the reference -> the chunk becomes garbage; if it has been
    // alive >= churnHoldMs under load it has already survived a young GC and been
    // promoted, so it is garbage IN OLD GEN); (2) top the ring back up toward
    // churnTargetBytes with fresh 256KiB chunks. When churnTargetBytes is 0, drain
    // everything immediately regardless of age. An allocation failure just stops adding
    // for this tick -- never throws up (the ring is bounded, so this should not happen,
    // but the try/catch is a safety net).
    private static void churnLoop() {
        while (true) {
            try {
                long target = churnTargetBytes;
                long holdMs = churnHoldMs;
                boolean linked = churnLinked;
                boolean dense = churnDense;
                int unitBytes = dense ? CHURN_DENSE_CLUSTER_BYTES : CHURN_CHUNK_BYTES;
                int addCap = dense ? 48 : 4096; // dense: cap the per-tick allocation burst, but
                                               // high enough to sustain the ring vs eviction
                                               // (~77 clusters/s needed; 48/tick @ 150ms = 320/s)
                long now = System.currentTimeMillis();
                synchronized (CHURN_LOCK) {
                    // evict aged-out (or everything, if target==0)
                    while (!CHURN_RING.isEmpty()) {
                        ChurnNode head = CHURN_RING.peekFirst();
                        long age = now - head.createdAtMs;
                        if (target == 0L || age >= holdMs) {
                            CHURN_RING.removeFirst();
                            head.refs = null; // evicted node must not keep other nodes alive
                            churnLiveBytes -= head.approxBytes;
                            if (churnLiveBytes < 0) churnLiveBytes = 0;
                            churnEvictions.incrementAndGet();
                        } else {
                            break; // head is the oldest; nothing behind it is older
                        }
                    }
                    // top up toward target
                    int added = 0;
                    while (churnLiveBytes + unitBytes <= target && added < addCap) {
                        try {
                            if (dense) {
                                DenseNode chain = null;
                                for (int k = 0; k < CHURN_DENSE_NODES; k++) {
                                    DenseNode node = new DenseNode(new int[CHURN_DENSE_NODE_INTS]);
                                    node.pad[0] = k; // touch so it is not a dead store
                                    node.next = chain;
                                    chain = node;
                                }
                                CHURN_RING.addLast(new ChurnNode(System.currentTimeMillis(),
                                    null, CHURN_EMPTY_REFS, chain, CHURN_DENSE_CLUSTER_BYTES));
                                churnLiveBytes += CHURN_DENSE_CLUSTER_BYTES;
                            } else {
                                byte[] payload = new byte[CHURN_CHUNK_BYTES];
                                payload[0] = (byte) 0xC7;
                                payload[payload.length - 1] = (byte) 0xC7;
                                Object[] refs = CHURN_EMPTY_REFS;
                                if (linked && !CHURN_RING.isEmpty()) {
                                    // snapshot up to CHURN_REF_WINDOW newest entries (tail-first),
                                    // then pick CHURN_REFS_PER_NODE at random from that window
                                    int winCap = Math.min(CHURN_REF_WINDOW, CHURN_RING.size());
                                    Object[] window = new Object[winCap];
                                    int wi = 0;
                                    java.util.Iterator<ChurnNode> it = CHURN_RING.descendingIterator();
                                    while (it.hasNext() && wi < winCap) window[wi++] = it.next();
                                    int want = Math.min(CHURN_REFS_PER_NODE, wi);
                                    refs = new Object[want];
                                    for (int r = 0; r < want; r++) refs[r] = window[CHURN_RANDOM.nextInt(wi)];
                                }
                                CHURN_RING.addLast(new ChurnNode(System.currentTimeMillis(),
                                    payload, refs, null, CHURN_CHUNK_BYTES));
                                churnLiveBytes += CHURN_CHUNK_BYTES;
                            }
                            added++;
                        } catch (OutOfMemoryError oom) {
                            lastError = "churn alloc failed at liveBytes=" + churnLiveBytes + ": " + oom;
                            break;
                        }
                    }
                }
                Thread.sleep(150);
            } catch (Throwable t) {
                lastError = "churn: " + t;
            }
        }
    }

    private static void handleChurn(String line, long cmdMtime) {
        String[] parts = line.split("\\s+");
        if (parts.length < 3) return;
        long mb;
        long holdMs;
        long ttlS = 60;
        long staticMb = -1;
        boolean linked = false;
        boolean dense = false;
        try {
            mb = Long.parseLong(parts[1]);
            holdMs = Long.parseLong(parts[2]);
            for (int i = 3; i < parts.length; i++) {
                if (parts[i].startsWith("ttl=")) ttlS = Long.parseLong(parts[i].substring(4));
                else if (parts[i].startsWith("static=")) staticMb = Long.parseLong(parts[i].substring(7));
                else if (parts[i].equalsIgnoreCase("linked")) linked = true;
                else if (parts[i].equalsIgnoreCase("dense")) dense = true;
            }
        } catch (NumberFormatException e) {
            return;
        }
        cmdMtimeAtRead = cmdMtime;
        cmdTtlS = ttlS;
        churnLinked = linked;
        churnDense = dense;
        churnTargetBytes = Math.max(0L, mb) * 1024L * 1024L;
        churnHoldMs = Math.max(250L, holdMs);
        applyStaticCompanion(staticMb);
    }

    // Shared: drive the existing retained-leak worker/governor path (like an ALLOCATE)
    // so old gen is pre-filled. staticMb < 0 => leave it alone.
    private static void applyStaticCompanion(long staticMb) {
        if (staticMb < 0) return;
        int reqMb = (int) staticMb;
        ABORT_ALLOCATION.set(false);
        if (reqMb != targetMb) {
            governorCeilingMb = 0;
            governorLastReleaseAt = 0;
            governorStableLowSinceMs = 0;
            govPrevStwSampledAt = 0;
            currentState = "ALLOCATING";
        }
        targetMb = reqMb;
    }

    // ================= Thread: graph (dense, constantly-rewritten reference graph) ======
    private static void graphLoop() {
        long lastTick = System.currentTimeMillis();
        while (true) {
            try {
                int slots = graphSlots;
                if (slots <= 0) {
                    if (graphBackbone != null) graphBackbone = null; // release -> GC reclaims
                    Thread.sleep(100);
                    lastTick = System.currentTimeMillis();
                    continue;
                }
                int edg = Math.max(0, graphEdges);
                Object[] bb = graphBackbone;
                if (bb == null || bb.length != slots) {
                    Object[] nb = new Object[slots];
                    for (int i = 0; i < slots; i++) {
                        try {
                            nb[i] = new GraphNode(new int[GRAPH_NODE_PAD_INTS],
                                edg > 0 ? new Object[edg] : CHURN_EMPTY_REFS);
                        } catch (OutOfMemoryError oom) {
                            lastError = "graph build OOM at slot " + i + "/" + slots;
                            break;
                        }
                    }
                    // wire initial edges once the array is populated
                    if (edg > 0) {
                        for (int i = 0; i < slots; i++) {
                            GraphNode n = (GraphNode) nb[i];
                            if (n == null) continue;
                            for (int k = 0; k < edg; k++) n.edges[k] = nb[GRAPH_WRITE_RANDOM.nextInt(slots)];
                        }
                    }
                    graphBackbone = nb;
                    bb = nb;
                }
                long now = System.currentTimeMillis();
                long dtMs = Math.max(1L, Math.min(200L, now - lastTick));
                lastTick = now;
                int writes = (int) Math.min(200000L, (long) graphWritesPerSec * dtMs / 1000L);
                int len = bb.length;
                for (int w = 0; w < writes; w++) {
                    try {
                        int idx = GRAPH_WRITE_RANDOM.nextInt(len);
                        // null the evicted node's outgoing edges -> it becomes a leaf, so it
                        // drops out of the live set once the ~edg nodes that still point AT
                        // it also age out. Without this, ~edg incoming edges per node keep
                        // the whole history transitively reachable from the backbone -> OOM.
                        Object old = bb[idx];
                        if (old instanceof GraphNode) {
                            ((GraphNode) old).edges = null;
                        }
                        Object[] e;
                        if (edg > 0) {
                            e = new Object[edg];
                            for (int k = 0; k < edg; k++) e[k] = bb[GRAPH_WRITE_RANDOM.nextInt(len)];
                        } else {
                            e = CHURN_EMPTY_REFS;
                        }
                        // the store below dirties this slot's card -> every young GC rescans it
                        bb[idx] = new GraphNode(new int[GRAPH_NODE_PAD_INTS], e);
                        graphWritesTotal.incrementAndGet();
                    } catch (OutOfMemoryError oom) {
                        lastError = "graph write OOM";
                        break;
                    }
                }
                Thread.sleep(10);
            } catch (Throwable t) {
                lastError = "graph: " + t;
            }
        }
    }

    private static void handleGraph(String line, long cmdMtime) {
        String[] parts = line.split("\\s+");
        if (parts.length < 4) return;
        long slotsK;
        long writesKPerSec;
        long edges;
        long ttlS = 60;
        long staticMb = -1;
        try {
            slotsK = Long.parseLong(parts[1]);
            writesKPerSec = Long.parseLong(parts[2]);
            edges = Long.parseLong(parts[3]);
            for (int i = 4; i < parts.length; i++) {
                if (parts[i].startsWith("ttl=")) ttlS = Long.parseLong(parts[i].substring(4));
                else if (parts[i].startsWith("static=")) staticMb = Long.parseLong(parts[i].substring(7));
            }
        } catch (NumberFormatException e) {
            return;
        }
        cmdMtimeAtRead = cmdMtime;
        cmdTtlS = ttlS;
        graphEdges = (int) Math.max(0L, Math.min(1000L, edges));
        graphWritesPerSec = (int) (Math.max(0L, Math.min(2000L, writesKPerSec)) * 1000L);
        graphSlots = (int) Math.max(0L, Math.min(20_000_000L, slotsK * 1000L));
        applyStaticCompanion(staticMb);
    }

    public static void premain(String agentArgs, Instrumentation inst) {
        try {
            if (CMD_FILE.exists()) {
                CMD_FILE.delete(); // startup stale-command protection
            }
            if (!CTL_DIR.exists()) CTL_DIR.mkdirs();
            registerGcListener();
            initUnsafe();
            initCodeCachePool();
            writeStatusBounded("agent started");
            writeBeat();

            startThread("wardence-leak-heartbeat", new Runnable() {
                public void run() { heartbeatLoop(); }
            });
            startThread("wardence-leak-control", new Runnable() {
                public void run() { controlLoop(); }
            });
            startThread("wardence-leak-worker", new Runnable() {
                public void run() { workerLoop(); }
            }, Thread.MAX_PRIORITY); // Kimi's Q1: "the single most important fix" under CPU-starved cgroups
            startThread("wardence-leak-watchdog", new Runnable() {
                public void run() { watchdogLoop(); }
            });
            startThread("wardence-leak-native", new Runnable() {
                public void run() { nativeLoop(); }
            });
            startThread("wardence-leak-refleak", new Runnable() {
                public void run() { refLeakLoop(); }
            });
            startThread("wardence-leak-codeleak", new Runnable() {
                public void run() { codeLeakLoop(); }
            });
            startThread("wardence-leak-churn", new Runnable() {
                public void run() { churnLoop(); }
            });
            startThread("wardence-leak-graph", new Runnable() {
                public void run() { graphLoop(); }
            });
            if (SYNC_ENABLED) {
                startThread("wardence-leak-reqsync", new Runnable() {
                    public void run() { requestSyncLoop(); }
                });
            }

            System.err.println("[wardence-leak-agent] hardened agent loaded, "
                + (SYNC_ENABLED ? 10 : 9) + " threads started"
                + " (reqsync=" + (SYNC_ENABLED ? "on" : "off")
                + ", governor=" + (GOVERNOR_PASSIVE ? "passive" : "active")
                + ", native=" + (UNSAFE != null ? "available" : "UNAVAILABLE") + ")");
        } catch (Throwable t) {
            // Never let the agent break the real app's boot.
            System.err.println("[wardence-leak-agent] premain failed (non-fatal): " + t);
        }
    }

    private static void startThread(String name, Runnable r) {
        startThread(name, r, Thread.NORM_PRIORITY);
    }

    private static void startThread(String name, Runnable r, int priority) {
        Thread t = new Thread(r, name);
        t.setDaemon(true);
        t.setPriority(priority);
        t.setUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
            public void uncaughtException(Thread thread, Throwable e) {
                lastError = thread.getName() + " died: " + e;
                System.err.println("[wardence-leak-agent] UNCAUGHT in " + thread.getName() + ": " + e);
                e.printStackTrace();
            }
        });
        t.start();
    }

    // ================= Thread 1: heartbeat =================
    // Does almost nothing -- wakes every 250ms, writes ONLY a tiny beat file. Never
    // parses commands, never allocates, never touches the (larger) status file. This is
    // what lets the watchdog/external observer distinguish "JVM alive, something else
    // stuck" (beat fresh) from "JVM/cgroup itself stalled" (beat also stale).
    private static void heartbeatLoop() {
        while (true) {
            try {
                heartbeatAt.set(System.currentTimeMillis());
                writeBeat();
                Thread.sleep(250);
            } catch (Throwable t) {
                lastError = "heartbeat: " + t;
            }
        }
    }

    // ================= Thread 2: control (cmd/status I/O) =================
    private static void controlLoop() {
        while (true) {
            try {
                controlLastRunAt.set(System.currentTimeMillis());

                Boolean cmdExists = boundedIo("cmd-exists", new Callable<Boolean>() {
                    public Boolean call() { return CMD_FILE.exists(); }
                });
                if (cmdExists == null) {
                    ioStuck.set(true);
                } else if (!cmdExists) {
                    ioStuck.set(false);
                    if ((allocatedBytes > 0 || nativeAllocatedBytes > 0 || refLeakCount > 0 || codeLeakCount > 0 || churnLiveBytes > 0 || graphSlots > 0) && !"RELEASING".equals(currentState)) {
                        requestRelease("no command file present");
                    }
                } else {
                    ioStuck.set(false);
                    final long mtime = CMD_FILE.lastModified();
                    if (mtime < AGENT_START_MS) {
                        boundedIo("cmd-delete-stale", new Callable<Object>() {
                            public Object call() { CMD_FILE.delete(); return null; }
                        });
                    } else {
                        List<String> lines = boundedIo("cmd-read", new Callable<List<String>>() {
                            public List<String> call() throws IOException { return Files.readAllLines(CMD_FILE.toPath()); }
                        });
                        if (lines == null) {
                            ioStuck.set(true);
                        } else {
                            String line = lines.isEmpty() ? "" : lines.get(0).trim();
                            if (line.isEmpty()) {
                                if (allocatedBytes > 0 || nativeAllocatedBytes > 0 || refLeakCount > 0 || codeLeakCount > 0 || churnLiveBytes > 0 || graphSlots > 0) requestRelease("empty command file");
                            } else if (line.equalsIgnoreCase("RELEASE")) {
                                // Act once per DISTINCT RELEASE command, matching how every
                                // other handler treats mtime -- see lastReleaseCmdMtime's own
                                // comment for the real stuck-in-RELEASING bug this fixes.
                                if (mtime != lastReleaseCmdMtime) {
                                    lastReleaseCmdMtime = mtime;
                                    requestRelease("RELEASE command");
                                }
                            } else if (line.toUpperCase().startsWith("ALLOCATE")) {
                                handleAllocate(line, mtime);
                            } else if (line.toUpperCase().startsWith("NATIVE")) {
                                handleNative(line, mtime);
                            } else if (line.toUpperCase().startsWith("REFLEAK")) {
                                handleRefLeak(line, mtime);
                            } else if (line.toUpperCase().startsWith("CODELEAK")) {
                                handleCodeLeak(line, mtime);
                            } else if (line.toUpperCase().startsWith("CHURN")) {
                                handleChurn(line, mtime);
                            } else if (line.toUpperCase().startsWith("GRAPH")) {
                                handleGraph(line, mtime);
                            }
                        }
                    }
                }

                writeStatusBounded(null);
                Thread.sleep(1000);
            } catch (Throwable t) {
                lastError = "control: " + t;
                System.err.println("[wardence-leak-agent] control loop error (continuing): " + t);
            }
        }
    }

    // Wraps a file op in a 2s-timeout task, per Qwen's point B -- prevents one blocked
    // file operation from freezing the control thread's own state machine. Returns null
    // (and marks ioStuck via the caller) on timeout/failure rather than throwing.
    private static <T> T boundedIo(String label, Callable<T> op) {
        long start = System.currentTimeMillis();
        Future<T> f = IO_POOL.submit(op);
        try {
            T result = f.get(2, TimeUnit.SECONDS);
            long elapsed = System.currentTimeMillis() - start;
            if (label.startsWith("cmd-read")) lastIoReadMs = elapsed;
            if (label.startsWith("status") || label.startsWith("beat")) lastIoWriteMs = elapsed;
            return result;
        } catch (TimeoutException e) {
            f.cancel(true);
            lastError = "io-timeout: " + label;
            return null;
        } catch (Exception e) {
            lastError = "io-error: " + label + ": " + e;
            return null;
        }
    }

    private static void handleAllocate(String line, long cmdMtime) {
        String[] parts = line.split("\\s+");
        if (parts.length < 2) return;
        int reqMb;
        long ttlS = 60;
        try {
            reqMb = Integer.parseInt(parts[1]);
            for (int i = 2; i < parts.length; i++) {
                if (parts[i].startsWith("ttl=")) {
                    ttlS = Long.parseLong(parts[i].substring(4));
                }
            }
        } catch (NumberFormatException e) {
            return;
        }
        cmdMtimeAtRead = cmdMtime;
        cmdTtlS = ttlS;
        long ageS = (System.currentTimeMillis() - cmdMtime) / 1000;
        if (ageS > ttlS) {
            requestRelease("TTL expired (age=" + ageS + "s > ttl=" + ttlS + "s)");
            return;
        }
        if (reqMb == lastFailedTargetMb) {
            return; // don't hot-loop retrying an already-OOM'd target
        }
        // Real, deliberate guard: the control loop re-reads and re-dispatches the SAME
        // still-present cmd file every ~1s for as long as it exists, so this method is
        // called far more often than "once per real ALLOCATE." Only reset governor state
        // on a GENUINELY NEW target -- otherwise the governor's reduced ceiling (the
        // whole point of the release-and-hold mechanism) would be wiped back to full
        // target on the very next tick, every time.
        boolean isNewTarget = (reqMb != targetMb);
        ABORT_ALLOCATION.set(false);
        targetMb = reqMb;
        if (isNewTarget) {
            governorCeilingMb = 0; // 0 = unrestricted, worker ramps freely toward the new target
            governorLastReleaseAt = 0;
            governorStableLowSinceMs = 0;
            govPrevStwSampledAt = 0; // force a fresh rolling-window baseline for the new hold
            // Real, related fix: only stamp ALLOCATING on a genuinely NEW target. This method
            // is re-invoked every ~1s for as long as the same cmd file persists -- unconditionally
            // setting currentState here would stomp the worker's own GOVERNED_HOLD/ALLOCATED
            // states back to ALLOCATING every single tick, making the governor's hold phases
            // invisible in the status output (the whole point of building it). Once a target is
            // active, currentState becomes the worker's to own exclusively.
            currentState = "ALLOCATING";
        }
    }

    private static void requestRelease(String reason) {
        ABORT_ALLOCATION.set(true);
        targetMb = 0;
        nativeTargetBytes = 0L; // native-memory mode is freed by nativeLoop() converging to 0
        refLeakTarget = 0L;     // refleak mode is freed by refLeakLoop() converging to 0
        codeLeakTarget = 0L;    // codeleak mode is freed by codeLeakLoop() dropping its loaders
        churnTargetBytes = 0L;  // churn ring is drained by churnLoop() when target is 0
        graphSlots = 0;         // graphLoop() nulls the backbone when slots is 0
        currentState = "RELEASING";
        lastError = reason;
    }

    // ================= Thread 3: worker (allocation only) =================
    // MAX_PRIORITY, no file I/O ever. Real, measured pacing added between chunks WHILE
    // RAMPING (see CHUNK_PACE_MS above) -- deliberately still NOT "Thread.sleep(1000)
    // in a CPU-starved cgroup" (Kimi's real warning, review 55 Q1): this is a bounded
    // 40ms pace, isolated to this one thread, checked against the abort flag on both
    // sides so RELEASE stays responsive within one pace interval, not the kind of long
    // blind sleep that caused the original single-threaded design's freeze.
    private static void workerLoop() {
        while (true) {
            try {
                int wantMb = targetMb;
                boolean releasing = "RELEASING".equals(currentState);
                if (releasing || wantMb == 0) {
                    if (allocatedBytes > 0) {
                        synchronized (CHUNKS_LOCK) {
                            CHUNKS.clear();
                        }
                        allocatedBytes = 0;
                        System.gc();
                        currentState = "IDLE";
                    } else if (!"IDLE".equals(currentState) && !"READY".equals(currentState)
                            && !"ERROR_OOM".equals(currentState) && !"WATCHDOG_RELEASE".equals(currentState)) {
                        currentState = "IDLE";
                    }
                    workerLastProgressAt.set(System.currentTimeMillis());
                } else {
                    // Governor ceiling (0 = unrestricted): the effective cap the worker is
                    // allowed to ramp toward right now, which can be BELOW the real target
                    // while the watchdog's governor is holding a reduced level after a
                    // release-step. This is what makes the spike-and-recover pattern real
                    // rather than a single monotonic ramp.
                    long govCeil = governorCeilingMb;
                    long wantBytes = (long) wantMb * 1024L * 1024L;
                    // In growth mode the worker keeps going PAST wantBytes toward ~97% of
                    // -Xmx; the governor's absolute ceiling (still active in passive mode)
                    // is what actually stops it. Non-growth mode: unchanged, capped at
                    // wantMb (or a governor-reduced ceiling).
                    long growCapMb = (GROWTH_MB_PER_SEC > 0)
                        ? (Runtime.getRuntime().maxMemory() / (1024L * 1024L)) * 97 / 100
                        : wantMb;
                    long effCeilMb = (govCeil > 0) ? govCeil : Math.max(wantMb, growCapMb);
                    long effCeilBytes = effCeilMb * 1024L * 1024L;
                    // Once past the initial target in growth mode, pace to the configured
                    // MiB/s instead of the fast CHUNK_PACE_MS ramp.
                    long chunkPace = (GROWTH_MB_PER_SEC > 0 && allocatedBytes >= wantBytes)
                        ? Math.max(1L, (long) CHUNK_BYTES * 1000L / (GROWTH_MB_PER_SEC * 1024L * 1024L))
                        : CHUNK_PACE_MS;

                    if (allocatedBytes < effCeilBytes && !ABORT_ALLOCATION.get()) {
                        try {
                            // Real object-graph chunk (was a flat byte[]): payload sized the
                            // same as before (CHUNK_BYTES, so all existing governor/ceiling
                            // arithmetic below is untouched), plus up to REFS_PER_NODE real
                            // references to already-retained, earlier nodes -- picked and
                            // linked inside the SAME lock scope as the list append (a
                            // deliberate widening of the lock's hold time vs. the old
                            // byte[]-only version, since picking a random existing Node
                            // safely requires the list to not be concurrently trimmed
                            // mid-pick; the added lock time is a single ~256KiB fill plus a
                            // handful of array reads, small relative to the existing 40ms
                            // pace between chunks).
                            byte[] payload = new byte[CHUNK_BYTES];
                            Arrays.fill(payload, (byte) 0xA5);
                            synchronized (CHUNKS_LOCK) {
                                int n = CHUNKS.size();
                                // Real hub-formation fix: only pick refs from a bounded
                                // sliding window of the most-recently-created nodes, never
                                // from the whole history -- otherwise the first few nodes
                                // (created when the pool was tiny) become referenced by
                                // nearly every later node, an unbounded-in-degree "hub"
                                // that dominates real pause cost almost immediately,
                                // independent of how big the eventual target is.
                                int windowSize = Math.min(n, REF_WINDOW_SIZE);
                                int windowStart = n - windowSize;
                                int refCount = Math.min(REFS_PER_NODE, windowSize);
                                Node[] refs = new Node[refCount];
                                for (int r = 0; r < refCount; r++) {
                                    refs[r] = CHUNKS.get(windowStart + GRAPH_RANDOM.nextInt(windowSize));
                                }
                                CHUNKS.add(new Node(payload, refs));
                            }
                            allocatedBytes += payload.length;
                            lastFailedTargetMb = -1;
                            workerLastProgressAt.set(System.currentTimeMillis());
                            if (allocatedBytes >= wantBytes && GROWTH_MB_PER_SEC > 0) {
                                // Past the initial target, still growing toward growCap.
                                currentState = "GROWING";
                            } else if (allocatedBytes >= wantBytes) {
                                currentState = "ALLOCATED"; // genuinely reached the real target
                            } else if (allocatedBytes >= effCeilBytes) {
                                // Reached the governor's current (reduced) ceiling, not yet the
                                // real target -- holding here until the watchdog lifts it.
                                currentState = "GOVERNED_HOLD";
                            }
                            if (!ABORT_ALLOCATION.get()) {
                                // Pace between chunks -- CHUNK_PACE_MS while ramping to the
                                // target, the growth-rate-derived pace once growing past it.
                                // Never on the release path (that stays as fast as possible).
                                // Re-checked right after waking so a RELEASE that arrived
                                // mid-pace is honored immediately.
                                try {
                                    Thread.sleep(chunkPace);
                                } catch (InterruptedException ignored) {
                                    Thread.currentThread().interrupt();
                                }
                            }
                        } catch (OutOfMemoryError oom) {
                            lastFailedTargetMb = wantMb;
                            currentState = "ERROR_OOM";
                            lastError = "OOM at allocatedBytes=" + allocatedBytes + " targetMb=" + wantMb;
                            System.err.println("[wardence-leak-agent] OutOfMemoryError: allocatedBytes=" + allocatedBytes
                                + " (" + (allocatedBytes / (1024 * 1024)) + "MiB), targetMb=" + wantMb);
                            workerLastProgressAt.set(System.currentTimeMillis());
                        }
                    } else {
                        // Idle: either genuinely at the real target, or capped at a reduced
                        // governor ceiling waiting for the watchdog to lift it. Mark the
                        // distinction so the status file honestly reflects which one this is.
                        if (governorCeilingMb > 0) {
                            // Held at a governor-reduced ceiling -- whether it was still
                            // ramping to target or (growth mode) had grown past it.
                            currentState = "GOVERNED_HOLD";
                        } else if (allocatedBytes >= wantBytes && GROWTH_MB_PER_SEC > 0) {
                            currentState = "GROWING"; // at growCap, not governor-clamped
                        } else if (allocatedBytes >= wantBytes) {
                            currentState = "ALLOCATED";
                        }
                        workerLastProgressAt.set(System.currentTimeMillis());
                        Thread.sleep(200); // idle poll, brief either way -- not a "surrender" under load
                    }
                }
            } catch (Throwable t) {
                lastError = "worker: " + t;
            }
        }
    }

    // ================= Thread 4: watchdog =================
    // Thresholds RELAXED (review 57, Kimi's Fix C -- a real, self-inflicted bug):
    // workerLastProgressAt is only updated AFTER `new byte[CHUNK_BYTES]` returns. If that
    // allocation triggers a multi-second GC pause -- entirely normal under exactly the
    // heap pressure this fault exists to create -- the old 5s threshold saw it as a
    // "stuck worker" and force-released the leak. The agent was aborting the very fault
    // it was injecting. The OLD single-threaded agent had no watchdog at all and simply
    // waited through GC pauses, which is a real part of why it looked better at 75MiB.
    // The watchdog's legitimate job is catching a genuinely wedged I/O/control path, not
    // policing GC pauses, so these are now well clear of any plausible pause duration.
    private static final long WORKER_STALL_ABORT_MS = 30000;
    private static final long CONTROL_STALL_ABORT_MS = 30000;

    private static void watchdogLoop() {
        while (true) {
            try {
                long now = System.currentTimeMillis();
                long hbAge = now - heartbeatAt.get();
                long ctrlAge = now - controlLastRunAt.get();
                long workerAge = now - workerLastProgressAt.get();

                if ("ALLOCATING".equals(currentState) && workerAge > WORKER_STALL_ABORT_MS) {
                    workerStuck.set(true);
                    forceRelease("watchdog: worker stalled " + workerAge + "ms while allocation active");
                } else {
                    workerStuck.set(false);
                }

                if (ctrlAge > CONTROL_STALL_ABORT_MS && hbAge < 5000) {
                    controlStuck.set(true);
                    if (allocatedBytes > 0) {
                        forceRelease("watchdog: control thread stalled " + ctrlAge + "ms, heartbeat still fresh");
                    }
                } else {
                    controlStuck.set(false);
                }

                if (cmdMtimeAtRead > 0) {
                    long cmdAgeS = (now - cmdMtimeAtRead) / 1000;
                    if (cmdAgeS > cmdTtlS && (allocatedBytes > 0 || nativeAllocatedBytes > 0 || refLeakCount > 0 || codeLeakCount > 0 || churnLiveBytes > 0 || graphSlots > 0)) {
                        forceRelease("watchdog: command TTL expired independently (age=" + cmdAgeS + "s)");
                    }
                }

                if (hbAge > 5000) {
                    lastError = "JVM_STALL suspected: heartbeat stale " + hbAge + "ms";
                }

                governorTick(now);

                Thread.sleep(1000);
            } catch (Throwable t) {
                lastError = "watchdog: " + t;
            }
        }
    }

    // Real spike-and-recover policy (review 57 follow-up, both Kimi and Qwen's
    // recommended path for a sustained 60s felt effect on this heap). Runs once per real
    // watchdog tick (~1s), but only actually SAMPLES/decides every GOVERNOR_ROLLING_
    // WINDOW_MS, since a 1s window is too noisy for a stable fraction. Only active while
    // a real target is set -- resets its own baseline cleanly whenever no leak is held.
    private static void governorTick(long now) {
        if (targetMb <= 0) {
            govPrevStwSampledAt = 0; // reset so the NEXT real ALLOCATE starts with a clean baseline
            return;
        }

        // Real, independent absolute-size trigger, checked EVERY tick (~1s), not gated
        // behind the 4s STW rolling window -- this is what actually reacts fast enough
        // to a rapid final ramp, which is the exact failure mode that let heap run past
        // the governor to the external ceiling twice in a row this session.
        long curPostGcMib = (lastPostGcHeapBytes < 0) ? -1 : (lastPostGcHeapBytes / (1024 * 1024));
        if (curPostGcMib >= GOVERNOR_ABS_HEAP_CEILING_MIB
                && (now - governorLastReleaseAt) >= GOVERNOR_MIN_MS_BETWEEN_RELEASES) {
            long effectiveCeilingNow = (governorCeilingMb > 0) ? governorCeilingMb : targetMb;
            long newCeilingAbs = Math.max(0, effectiveCeilingNow - GOVERNOR_RELEASE_STEP_MB);
            governorTrimTo(newCeilingAbs);
            governorCeilingMb = newCeilingAbs;
            governorLastReleaseAt = now;
            governorStableLowSinceMs = 0;
            governorReleaseEvents.incrementAndGet();
            lastError = "governor: released " + GOVERNOR_RELEASE_STEP_MB + "MiB (ABSOLUTE post-GC heap="
                + curPostGcMib + "MiB >= " + GOVERNOR_ABS_HEAP_CEILING_MIB + "MiB, size-trigger not STW%),"
                + " new ceiling=" + newCeilingAbs + "MiB";
            // A size-triggered release already changed the ceiling this tick -- let the
            // STW-based logic below re-evaluate fresh on the NEXT tick against the new
            // reduced ceiling, rather than potentially firing twice in the same instant.
            return;
        }

        // Passive mode: the absolute-ceiling branch above is the ONLY actor. Skip
        // both the STW%-pressure release and its paired recovery/re-ramp logic --
        // see GOVERNOR_PASSIVE's own comment for why. Returning here (rather than
        // just skipping the release branch) is deliberate: the recovery branch only
        // exists to undo an STW-triggered trim, so running it alone would be dead
        // logic that could still lift a ceiling the absolute backstop had lowered
        // for a real reason.
        if (GOVERNOR_PASSIVE) {
            return;
        }

        long nowStw = stwPauseMsTotal.get();
        if (govPrevStwSampledAt == 0) {
            govPrevStwMs = nowStw;
            govPrevStwSampledAt = now;
            return;
        }
        long dT = now - govPrevStwSampledAt;
        if (dT < GOVERNOR_ROLLING_WINDOW_MS) {
            return; // not enough real time elapsed yet for a trustworthy rolling fraction
        }
        long dStw = nowStw - govPrevStwMs;
        long stwPct = (dT > 0) ? (dStw * 100 / dT) : 0;
        govPrevStwMs = nowStw;
        govPrevStwSampledAt = now;

        long effectiveCeiling = (governorCeilingMb > 0) ? governorCeilingMb : targetMb;

        if (stwPct >= GOVERNOR_HIGH_STW_PCT
                && (now - governorLastReleaseAt) >= GOVERNOR_MIN_MS_BETWEEN_RELEASES) {
            long newCeiling = Math.max(0, effectiveCeiling - GOVERNOR_RELEASE_STEP_MB);
            governorTrimTo(newCeiling);
            governorCeilingMb = newCeiling;
            governorLastReleaseAt = now;
            governorStableLowSinceMs = 0;
            governorReleaseEvents.incrementAndGet();
            lastError = "governor: released " + GOVERNOR_RELEASE_STEP_MB + "MiB (rolling STW=" + stwPct
                + "% >= " + GOVERNOR_HIGH_STW_PCT + "%), new ceiling=" + newCeiling + "MiB";
        } else if (stwPct < GOVERNOR_LOW_STW_PCT) {
            if (governorStableLowSinceMs == 0) {
                governorStableLowSinceMs = now;
            } else if ((now - governorStableLowSinceMs) >= GOVERNOR_MIN_STABLE_LOW_MS
                    && (now - governorLastReleaseAt) >= GOVERNOR_MIN_MS_BETWEEN_RELEASES
                    && effectiveCeiling < targetMb) {
                // Real recovery confirmed for a sustained real interval -- lift the ceiling
                // back to the true target. Regrowth rate is still bounded by the worker's
                // own CHUNK_PACE_MS, so this doesn't re-introduce a fast/uncontrolled ramp.
                governorCeilingMb = targetMb;
                governorStableLowSinceMs = 0;
            }
        } else {
            governorStableLowSinceMs = 0; // in between the two thresholds -- not calm enough to count
        }
    }

    // Partial release, owned by the governor -- distinct from forceRelease()/RELEASE,
    // which clear everything. Trims only down to the new ceiling, under the same lock
    // the worker uses, so a concurrent append from the worker can never race with this.
    private static void governorTrimTo(long newCeilingMb) {
        long newCeilingBytes = Math.max(0, newCeilingMb) * 1024L * 1024L;
        synchronized (CHUNKS_LOCK) {
            while (allocatedBytes > newCeilingBytes && !CHUNKS.isEmpty()) {
                CHUNKS.remove(CHUNKS.size() - 1);
                allocatedBytes -= CHUNK_BYTES;
            }
            if (allocatedBytes < 0) allocatedBytes = 0;
        }
    }

    private static void forceRelease(String reason) {
        synchronized (CHUNKS_LOCK) {
            CHUNKS.clear();
        }
        allocatedBytes = 0;
        targetMb = 0;
        nativeTargetBytes = 0L; // nativeLoop() frees its chunks as it converges to 0
        refLeakTarget = 0L;     // refLeakLoop() drops its soft refs as it converges to 0
        codeLeakTarget = 0L;    // codeLeakLoop() drops its loaders as it converges to 0
        churnTargetBytes = 0L;  // churnLoop() drains its ring when target is 0
        graphSlots = 0;         // graphLoop() nulls the backbone when slots is 0
        ABORT_ALLOCATION.set(true);
        currentState = "WATCHDOG_RELEASE";
        lastError = reason;
        watchdogReleases.incrementAndGet();
        System.err.println("[wardence-leak-agent] " + reason);
    }

    // ================= Thread 5: request-synced GC-pressure trigger =================
    // v2 (see SYNC_REQUEST_PROCESSOR_MBEAN's own comment for the real,
    // measured reason v1's live busy-edge watch was replaced): watches
    // Tomcat's own real, monotonic GlobalRequestProcessor.requestCount and
    // fires whenever it notices the count has genuinely increased since
    // the last successful read -- immune to this same thread being frozen
    // for however long, since the counter itself never "resets" or gets
    // missed the way a live instantaneous flag could.
    private static void requestSyncLoop() {
        MBeanServer mbs = ManagementFactory.getPlatformMBeanServer();
        ObjectName processorName;
        try {
            processorName = new ObjectName(SYNC_REQUEST_PROCESSOR_MBEAN);
        } catch (Throwable t) {
            // Real, non-fatal degrade: if the MBean name is ever wrong for
            // a different shipping build, this thread simply never fires
            // rather than crashing the agent or the app it's attached to.
            syncMbeanUnavailable.set(true);
            lastError = "reqsync: MBean lookup failed, thread disabled: " + t;
            return;
        }

        // -1 sentinel: "not yet read for real" -- the first successful
        // read seeds this WITHOUT firing (there's no real "increase" to
        // react to on the very first observation, only a baseline to
        // compare future reads against).
        long prevRequestCount = -1L;
        long lastLoopAtMs = System.currentTimeMillis();
        while (true) {
            try {
                // Real stall measurement -- see SYNC_STALL_THRESHOLD_MS's
                // own comment above for why this exists. Computed BEFORE
                // any real work this iteration, against the previous
                // iteration's own start time, so it captures the real
                // elapsed wall-clock gap (sleep + any freeze) rather than
                // just this iteration's own work time.
                long loopStartMs = System.currentTimeMillis();
                long cycleMs = loopStartMs - lastLoopAtMs;
                lastLoopAtMs = loopStartMs;
                long stallMs = cycleMs - SYNC_POLL_MS;
                if (stallMs > SYNC_STALL_THRESHOLD_MS) {
                    syncStallCount.incrementAndGet();
                    syncStallMsTotal.addAndGet(stallMs);
                    if (stallMs > syncMaxStallMs) {
                        syncMaxStallMs = stallMs;
                    }
                    System.err.println("[wardence-leak-agent] reqsync STALL: " + stallMs
                        + "ms over expected (likely this thread was itself frozen by a real "
                        + "GC pause) at=" + loopStartMs);
                }

                Object countObj = mbs.getAttribute(processorName, "requestCount");
                long nowCount = (countObj instanceof Number) ? ((Number) countObj).longValue() : -1L;
                if (nowCount >= 0) {
                    if (prevRequestCount >= 0 && nowCount > prevRequestCount) {
                        // One burst per real OBSERVATION of new traffic, not
                        // one per individual completed request -- if several
                        // requests completed during a real freeze, they're
                        // still just "traffic happened, react once," same as
                        // v1's own edge-per-observation shape.
                        maybeFireSyncBurst();
                    }
                    prevRequestCount = nowCount;
                }
                Thread.sleep(SYNC_POLL_MS);
            } catch (Throwable t) {
                // A single bad MBean read shouldn't permanently disable this
                // thread the way a lookup failure above does -- back off a
                // beat and keep polling. lastLoopAtMs is re-stamped here too
                // (not just at the top of the try) so this deliberate 1s
                // backoff is never itself misattributed as a real stall on
                // the next iteration.
                lastError = "reqsync: " + t;
                syncMbeanUnavailable.set(true);
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
                lastLoopAtMs = System.currentTimeMillis();
                continue;
            }
            syncMbeanUnavailable.set(false);
        }
    }

    // Fires a real, throwaway (never retained in CHUNKS) allocation burst
    // sized against REAL-TIME free heap, never System.gc() -- preserves
    // the real "Allocation Failure" GC-cause signature (System.gc()'s own
    // distinct GC-cause would be a real, inspectable tell that the pause
    // was engineered, the exact blinding leak review 61 v1 had and v2
    // fixed).
    private static void maybeFireSyncBurst() {
        long now = System.currentTimeMillis();
        if (now - syncLastTriggerAtMs < SYNC_DEBOUNCE_MS) {
            return;
        }
        // Only meaningful while a real leak target is active -- this is
        // part of the memory-leak fault mechanism, never a general
        // shipping behavior that could fire outside an episode.
        if (targetMb <= 0) {
            return;
        }

        Runtime rt = Runtime.getRuntime();
        long freeMib = (rt.maxMemory() - (rt.totalMemory() - rt.freeMemory())) / (1024 * 1024);
        // Bounded by SYNC_BURST_MAX_MIB, not just headroom -- see that
        // constant's own comment for why an unbounded (freeMib - margin)
        // burst is a real problem, not just a theoretical one.
        long burstMib = Math.min(freeMib - SYNC_BURST_MARGIN_MIB, SYNC_BURST_MAX_MIB);
        if (burstMib <= 0) {
            syncSkippedNoHeadroomCount.incrementAndGet();
            return;
        }

        // Real per-event visibility (added 2026-08-22, per real tuning
        // session need): a cumulative counter alone couldn't distinguish
        // "debounce is suppressing real requests" from "fewer requests
        // arrived this run" -- this line, tailed live via `kubectl logs
        // -f`, gives the real gap-since-last-trigger directly, comparable
        // against SYNC_DEBOUNCE_MS. gapMs is 0 for the very first real
        // trigger of an episode (syncLastTriggerAtMs starts at 0), which
        // is expected and not a real "instant re-trigger."
        long gapMs = (syncLastTriggerAtMs == 0) ? 0 : (now - syncLastTriggerAtMs);
        System.err.println("[wardence-leak-agent] reqsync FIRE: gapMs=" + gapMs
            + " burstMib=" + burstMib + " freeMib=" + freeMib
            + " heapUsedMib=" + ((rt.totalMemory() - rt.freeMemory()) / (1024 * 1024))
            + " at=" + now);

        // Debounce timestamp is set BEFORE the allocation attempt, not
        // after -- a burst that throws OOM below still genuinely disturbed
        // the heap and should still hold the debounce window, not retry
        // immediately.
        syncLastTriggerAtMs = now;
        try {
            // Built from the SAME CHUNK_BYTES-sized pieces the main leak
            // mechanism uses (never one giant array) -- a single byte[] at
            // burstMib scale would be a real humongous object (>=512KiB,
            // see CHUNK_BYTES's own comment above), placed directly in
            // old-gen and reclaimable only by a full/major GC instead of
            // the brief, request-scoped pause this mechanism is meant to
            // produce. Held in a local list (never CHUNKS, never a static
            // field) so every piece goes out of scope together the instant
            // this method returns -- real, throwaway garbage for the next
            // collection, nothing retained.
            long burstBytes = burstMib * 1024L * 1024L;
            int numChunks = (int) (burstBytes / CHUNK_BYTES);
            List<byte[]> burst = new ArrayList<byte[]>(numChunks);
            for (int i = 0; i < numChunks; i++) {
                byte[] chunk = new byte[CHUNK_BYTES];
                Arrays.fill(chunk, (byte) 0xA5);
                burst.add(chunk);
            }
            syncTriggerCount.incrementAndGet();
            // Defeats dead-store elimination (a JIT that proves `burst` is
            // never read again could otherwise skip the fill/allocation
            // entirely) without retaining anything anywhere.
            if (!burst.isEmpty() && (burst.get(0)[0] & 0xFF) != 0xA5) {
                System.err.println("[wardence-leak-agent] reqsync: unexpected burst fill byte");
            }
        } catch (OutOfMemoryError oom) {
            // Real, non-fatal: the free-heap estimate above was racy/stale
            // (another allocator moved the goalposts between the read and
            // the allocation) -- skip this trigger, the debounce window
            // still holds so this doesn't hot-loop retrying.
            lastError = "reqsync: OOM during sync burst (non-fatal, estimate was stale)";
        }
    }

    // ================= status/beat I/O =================
    private static void writeBeat() {
        try {
            if (!CTL_DIR.exists()) CTL_DIR.mkdirs();
            FileWriter fw = new FileWriter(BEAT_TMP);
            try {
                fw.write(String.valueOf(System.currentTimeMillis()));
            } finally {
                fw.close();
            }
            BEAT_TMP.renameTo(BEAT_FILE);
        } catch (IOException e) {
            // best-effort -- a failed beat write should never crash the agent
        }
    }

    private static void writeStatusBounded(String note) {
        boundedIo("status-write", new Callable<Object>() {
            public Object call() { writeStatus(note); return null; }
        });
    }

    private static void writeStatus(String note) {
        try {
            if (!CTL_DIR.exists()) CTL_DIR.mkdirs();
            Runtime rt = Runtime.getRuntime();
            long gcCount = 0, gcTimeMs = 0;
            for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) {
                gcCount += b.getCollectionCount();
                gcTimeMs += b.getCollectionTime();
            }
            // Kimi's Fix D (review 57): capture the sampling instant IMMEDIATELY after
            // reading the GC counters, from the JVM's OWN clock. This is what lets the
            // bash harness compute a GC-time fraction over the real interval between two
            // samples, instead of over its own assumed loop cadence -- immune to both
            // bash-loop delay and to a status write that stalls before landing on disk.
            long gcSampledAt = System.currentTimeMillis();
            long now = gcSampledAt;
            long ttlRemainingS = cmdMtimeAtRead > 0
                ? Math.max(0, cmdTtlS - (now - cmdMtimeAtRead) / 1000)
                : 0;

            StringBuilder sb = new StringBuilder();
            sb.append("version=2\n");
            sb.append("state=").append(currentState).append('\n');
            sb.append("requested_mb=").append(targetMb).append('\n');
            sb.append("allocated_mb=").append(allocatedBytes / (1024 * 1024)).append('\n');
            sb.append("heap_used_mib=").append((rt.totalMemory() - rt.freeMemory()) / (1024 * 1024)).append('\n');
            sb.append("heap_max_mib=").append(rt.maxMemory() / (1024 * 1024)).append('\n');
            sb.append("gc_count=").append(gcCount).append('\n');
            sb.append("gc_time_ms=").append(gcTimeMs).append('\n');
            // review 57 additions -- see the field declarations above for why all four exist
            sb.append("gc_sampled_at_ms=").append(gcSampledAt).append('\n');
            sb.append("stw_pause_ms=").append(stwPauseMsTotal.get()).append('\n');
            sb.append("gc_notifications=").append(gcNotificationCount.get()).append('\n');
            sb.append("post_gc_heap_mib=").append(
                lastPostGcHeapBytes < 0 ? -1 : (lastPostGcHeapBytes / (1024 * 1024))).append('\n');
            sb.append("governor_ceiling_mb=").append(governorCeilingMb).append('\n');
            sb.append("governor_release_events=").append(governorReleaseEvents.get()).append('\n');
            // Real deployed-config readback (2026-08-29). Both flags are set via
            // -D JVM system properties in JAVA_OPTS, which is exactly the kind of
            // change that silently fails to land (wrong checkout, un-rolled pod,
            // typo'd property name) and then gets misread as "the tuning didn't
            // work." Reporting the values the JVM ACTUALLY resolved makes that a
            // one-line check instead of a wasted tuning round.
            sb.append("governor_mode=").append(GOVERNOR_PASSIVE ? "passive" : "active").append('\n');
            sb.append("governor_abs_ceiling_mib=").append(GOVERNOR_ABS_HEAP_CEILING_MIB).append('\n');
            sb.append("reqsync_enabled=").append(SYNC_ENABLED).append('\n');
            sb.append("native_mb=").append(nativeAllocatedBytes / (1024 * 1024)).append('\n');
            sb.append("native_target_mb=").append(nativeTargetBytes / (1024 * 1024)).append('\n');
            sb.append("native_alloc_failed=").append(nativeAllocFailed).append('\n');
            sb.append("refleak_count=").append(refLeakCount).append('\n');
            sb.append("refleak_target=").append(refLeakTarget).append('\n');
            sb.append("codeleak_count=").append(codeLeakCount).append('\n');
            sb.append("codeleak_target=").append(codeLeakTarget).append('\n');
            sb.append("codeleak_failed=").append(codeLeakFailed).append('\n');
            sb.append("churn_target_mb=").append(churnTargetBytes / (1024 * 1024)).append('\n');
            sb.append("churn_live_mb=").append(churnLiveBytes / (1024 * 1024)).append('\n');
            sb.append("churn_hold_ms=").append(churnHoldMs).append('\n');
            sb.append("churn_linked=").append(churnLinked).append('\n');
            sb.append("churn_dense=").append(churnDense).append('\n');
            sb.append("graph_slots=").append(graphSlots).append('\n');
            sb.append("graph_edges=").append(graphEdges).append('\n');
            sb.append("graph_writes=").append(graphWritesTotal.get()).append('\n');
            sb.append("churn_evictions=").append(churnEvictions.get()).append('\n');
            if (CODE_CACHE_POOL != null) {
                try {
                    java.lang.management.MemoryUsage cu = CODE_CACHE_POOL.getUsage();
                    long used = cu.getUsed(), max = cu.getMax();
                    sb.append("codecache_used_mib=").append(used / (1024 * 1024)).append('\n');
                    sb.append("codecache_max_mib=").append(max > 0 ? (max / (1024 * 1024)) : -1).append('\n');
                    sb.append("codecache_pct=").append(max > 0 ? (used * 100 / max) : -1).append('\n');
                } catch (Throwable ignored) {
                    sb.append("codecache_used_mib=-1\ncodecache_max_mib=-1\ncodecache_pct=-1\n");
                }
            } else {
                sb.append("codecache_used_mib=-1\ncodecache_max_mib=-1\ncodecache_pct=-1\n");
            }
            sb.append("heartbeat_age_ms=").append(now - heartbeatAt.get()).append('\n');
            sb.append("control_age_ms=").append(now - controlLastRunAt.get()).append('\n');
            sb.append("worker_progress_age_ms=").append(now - workerLastProgressAt.get()).append('\n');
            sb.append("io_stuck=").append(ioStuck.get()).append('\n');
            sb.append("worker_stuck=").append(workerStuck.get()).append('\n');
            sb.append("control_stuck=").append(controlStuck.get()).append('\n');
            sb.append("watchdog_releases=").append(watchdogReleases.get()).append('\n');
            sb.append("sync_trigger_count=").append(syncTriggerCount.get()).append('\n');
            sb.append("sync_skipped_no_headroom_count=").append(syncSkippedNoHeadroomCount.get()).append('\n');
            sb.append("sync_mbean_unavailable=").append(syncMbeanUnavailable.get()).append('\n');
            sb.append("sync_stall_count=").append(syncStallCount.get()).append('\n');
            sb.append("sync_stall_ms_total=").append(syncStallMsTotal.get()).append('\n');
            sb.append("sync_max_stall_ms=").append(syncMaxStallMs).append('\n');
            sb.append("last_io_read_ms=").append(lastIoReadMs).append('\n');
            sb.append("last_io_write_ms=").append(lastIoWriteMs).append('\n');
            sb.append("ttl_remaining_s=").append(ttlRemainingS).append('\n');
            sb.append("last_error=").append(lastError == null ? "" : lastError.replace('\n', ' ')).append('\n');
            sb.append("note=").append(note == null ? "" : note).append('\n');
            sb.append("updated_at=").append(now).append('\n');
            FileWriter fw = new FileWriter(STATUS_TMP);
            try {
                fw.write(sb.toString());
            } finally {
                fw.close();
            }
            STATUS_TMP.renameTo(STATUS_FILE);
        } catch (IOException e) {
            // best-effort -- a failed status write should never crash the agent
        }
    }
}
