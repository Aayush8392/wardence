"""
Real, ONE-TIME production installer for memory-leak's JVM-attach LeakAgent
(2026-08-21 session, LOCKED design -- see wardence_buildlog.md for the full
measurement/design chain). Attaches wardence/LeakAgent.java (already
extracted verbatim, byte-identical, to wardence/LeakAgent.java -- confirmed
via checksum against the clone that validated it) to the REAL production
`shipping` Deployment in `sock-shop`.

Run ONCE, by the user, from a terminal -- never executed by Claude, per this
project's own standing rule. Two modes:
    python install_shipping_leak_agent.py            # install
    python install_shipping_leak_agent.py --rollback  # revert to pre-install spec

Real, deliberate structure, following the exact validated build/attach
mechanics from check_memory_leak_javaagent_hardened.sh's STAGE A/B/C (the
clone-testing harness this whole design was measured against) -- NOT a
fresh reimplementation, the same real steps applied to a persistent
Deployment instead of a throwaway clone:
    1. Build the real jar via a throwaway builder pod (reuses shipping's
       own live image, guarantees toolchain match -- same reasoning as the
       clone script).
    2. Pre-flight validation (Kimi's review-59 recommendation, complementary
       to Qwen's rollout-strategy mitigation, not competing): attach the
       agent to a THROWAWAY standalone pod first (same image, same jar),
       confirm it genuinely loads (status+beat files appear) and a real
       ALLOCATE/RELEASE round-trip works, BEFORE touching the real
       Deployment at all.
    3. Capture the real, current `shipping` Deployment spec to a local,
       timestamped JSON file -- this is the real rollback path, an exact
       revert, not a guess at what the original looked like.
    4. Patch the real Deployment: JAVA_OPTS gains ONLY `-javaagent:...`
       (locked decision: no GC-logging flags, no -Xmx/-Xms change --
       production's real 128m/64m heap stays untouched), the same real
       agent-jar/agent-ctl volumes+initContainer the clone validated, and
       the locked rollout-safety strategy (minReadySeconds=400,
       maxSurge=1, maxUnavailable=0 -- real measured shipping boot times
       were 285-335s, 400s gives real margin; the surge-then-drain shape
       avoids a real outage window on this single-replica Deployment,
       confirmed via check_bad_rollout_readyreplicas.py's own finding that
       replicas=1 forces maxSurge-first RollingUpdate behavior).
    5. Wait for the real rollout, then the same real active-HTTP-readiness
       poll the clone script uses (shipping has no readinessProbe --
       `rollout status` alone only means the container process started,
       not that Spring Boot finished booting) and the same STAGE C
       status+beat confirmation.

Real, honest scope boundary: this installs the MECHANISM only. It does NOT
reset memory-leak's earned trust state (a separate, explicit action item --
both the mechanism AND the diagnosis signal changed, so the old streak's
meaning doesn't carry over) and does NOT retarget any already-recorded
episode data. Those remain real, separate, not-yet-done items.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE = "sock-shop"
TARGET = "shipping"
CONTAINER = "shipping"
CONFIGMAP_NAME = "shipping-leak-agent-jar"
BUILDER_NAME = "wardence-leak-agent-builder"
PREFLIGHT_NAME = "wardence-leak-agent-preflight"
BACKUP_DIR = Path.home() / "wardence_p2_data" / "shipping_deployment_backups"
LOCAL_JAR = "/tmp/wardence_leak_agent_install.jar"
FRONT_END_LABEL = "front-end"

# Real, locked (session 2026-08-21, "3 real numeric decisions LOCKED"):
MIN_READY_SECONDS = 400
READY_WAIT_S = 400  # matches the clone script's own real active-poll budget


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def kubectl_exec(pod: str, container: str | None, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "exec", "-n", NAMESPACE, pod]
    if container is not None:
        cmd += ["-c", container]
    cmd += ["--"] + args
    return run(cmd, **kwargs)


def build_jar() -> None:
    """Real jar build via a throwaway builder pod, same STAGE A mechanics
    already validated in check_memory_leak_javaagent_hardened.sh -- reuses
    shipping's own live image (guarantees toolchain/JDK match) rather than
    a generic Java base image."""
    print("=== Deleting any leftover builder pod from a prior run ===")
    run(["kubectl", "delete", "pod", BUILDER_NAME, "-n", NAMESPACE,
         "--ignore-not-found=true", "--wait=true", "--timeout=60s"])

    image_result = run([
        "kubectl", "get", "deployment", TARGET, "-n", NAMESPACE,
        "-o", "jsonpath={.spec.template.spec.containers[0].image}",
    ])
    shipping_image = image_result.stdout.strip()
    if not shipping_image:
        print("ABORT: could not read shipping's real current image -- cannot start the builder pod.")
        sys.exit(1)
    print(f"real shipping image (reused for the builder pod): {shipping_image}")

    print("=== Starting builder pod ===")
    run(["kubectl", "run", BUILDER_NAME, "-n", NAMESPACE, f"--image={shipping_image}",
         "--restart=Never", "--", "sh", "-c", "sleep 3600"])
    ready = run(["kubectl", "wait", "--for=condition=Ready", f"pod/{BUILDER_NAME}",
                 "-n", NAMESPACE, "--timeout=120s"])
    if ready.returncode != 0:
        print("ABORT: builder pod never became Ready.")
        sys.exit(1)

    print("=== Streaming LeakAgent.java + manifest into the builder pod ===")
    java_src = Path(__file__).parent / "wardence" / "LeakAgent.java"
    manifest = "Manifest-Version: 1.0\nPremain-Class: wardence.LeakAgent\n\n"
    kubectl_exec(BUILDER_NAME, None, ["mkdir", "-p", "/tmp/wardence"])
    # Real, deliberate encoding="utf-8" on both text-mode calls below --
    # subprocess.run(text=True) without an explicit encoding uses the
    # platform's locale-preferred encoding, which on Windows is often NOT
    # UTF-8 (frequently cp1252), while str.encode() always defaults to
    # UTF-8. Left implicit, the local_bytes size check a few lines down
    # (which uses .encode(), always UTF-8) could silently mismatch what
    # was actually SENT here if the source ever contained non-ASCII bytes.
    # Currently moot (LeakAgent.java confirmed pure ASCII, where every
    # encoding agrees), but pinned explicitly rather than relying on that
    # coincidence.
    write_java = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NAMESPACE, BUILDER_NAME, "--", "sh", "-c",
         "cat > /tmp/wardence/LeakAgent.java"],
        input=java_src.read_text(encoding="utf-8"), text=True, encoding="utf-8", capture_output=True,
    )
    if write_java.returncode != 0:
        print(f"ABORT: streaming LeakAgent.java into the builder pod failed: {write_java.stderr}")
        sys.exit(1)
    write_manifest = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NAMESPACE, BUILDER_NAME, "--", "sh", "-c",
         "cat > /tmp/manifest.mf"],
        input=manifest, text=True, encoding="utf-8", capture_output=True,
    )
    if write_manifest.returncode != 0:
        print(f"ABORT: streaming manifest.mf into the builder pod failed: {write_manifest.stderr}")
        sys.exit(1)

    # Real transfer-size check, same discipline as the clone script -- catch
    # a truncated/corrupted transfer before wasting time on a bad compile.
    local_bytes = len(java_src.read_text(encoding="utf-8").encode("utf-8"))
    remote_bytes_result = kubectl_exec(BUILDER_NAME, None, ["wc", "-c", "/tmp/wardence/LeakAgent.java"])
    remote_bytes = remote_bytes_result.stdout.split()[0] if remote_bytes_result.stdout.split() else None
    if remote_bytes is None or int(remote_bytes) != local_bytes:
        print(f"ABORT: source file transfer size mismatch (local={local_bytes}, "
              f"remote={remote_bytes}) -- aborting before a bad compile.")
        sys.exit(1)

    print("=== Compiling + jarring inside the builder pod ===")
    build = kubectl_exec(BUILDER_NAME, None, [
        "sh", "-c",
        "cd /tmp && mkdir -p classes && javac -d classes wardence/LeakAgent.java "
        "&& jar cmf manifest.mf leak-agent.jar -C classes . && echo BUILD_OK",
    ])
    if build.returncode != 0 or "BUILD_OK" not in build.stdout:
        print(f"ABORT: agent build failed inside the builder pod.\nstdout: {build.stdout}\nstderr: {build.stderr}")
        sys.exit(1)
    print("  build OK")

    print(f"=== Streaming the built jar back to local disk: {LOCAL_JAR} ===")
    # Real, deliberate: NOT run() -- that helper sets text=True, which
    # decodes stdout as text and would corrupt a binary .jar (there is no
    # safe way to decode-then-reencode arbitrary binary bytes through a
    # text codec). Raw bytes mode here, matching the clone script's own
    # `kubectl exec ... -- cat ... > file` shell redirection, which never
    # passes through a text decode either.
    cat_jar = subprocess.run(
        ["kubectl", "exec", "-n", NAMESPACE, BUILDER_NAME, "--", "cat", "/tmp/leak-agent.jar"],
        capture_output=True,
    )
    if cat_jar.returncode != 0 or not cat_jar.stdout:
        print(f"ABORT: could not stream the built jar back locally: {cat_jar.stderr.decode(errors='replace')}")
        sys.exit(1)
    Path(LOCAL_JAR).write_bytes(cat_jar.stdout)
    remote_jar_bytes = kubectl_exec(BUILDER_NAME, None, ["wc", "-c", "/tmp/leak-agent.jar"]).stdout.split()
    local_jar_bytes = Path(LOCAL_JAR).stat().st_size
    if not remote_jar_bytes or local_jar_bytes == 0 or int(remote_jar_bytes[0]) != local_jar_bytes:
        print(f"ABORT: jar transfer size mismatch (local={local_jar_bytes}, "
              f"remote={remote_jar_bytes}) -- aborting rather than proceeding with a bad jar.")
        sys.exit(1)
    print(f"  jar OK, {local_jar_bytes} bytes")

    print("=== Cleaning up the builder pod ===")
    run(["kubectl", "delete", "pod", BUILDER_NAME, "-n", NAMESPACE, "--ignore-not-found=true", "--wait=false"])


def preflight_validate() -> None:
    """Kimi's real, complementary review-59 recommendation: attach the
    agent to a THROWAWAY standalone pod first (same image, same jar,
    JAVA_OPTS patched the identical way production will be), confirm it
    genuinely loads and a real ALLOCATE/RELEASE round-trip works -- BEFORE
    the real Deployment is ever touched. Cheap insurance against a bad
    jar/JAVA_OPTS interaction that would otherwise only be discovered on
    the real production pod."""
    print("\n=== PRE-FLIGHT: validating the built jar on a throwaway standalone pod first ===")
    run(["kubectl", "delete", "pod", PREFLIGHT_NAME, "-n", NAMESPACE,
         "--ignore-not-found=true", "--wait=true", "--timeout=60s"])

    image_result = run([
        "kubectl", "get", "deployment", TARGET, "-n", NAMESPACE,
        "-o", "jsonpath={.spec.template.spec.containers[0].image}",
    ])
    shipping_image = image_result.stdout.strip()
    java_opts_result = run([
        "kubectl", "get", "deployment", TARGET, "-n", NAMESPACE,
        "-o", 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="JAVA_OPTS")].value}',
    ])
    orig_java_opts = java_opts_result.stdout.strip()
    # Real, deliberate: must build this IDENTICALLY to patch_deployment()'s
    # own JAVA_OPTS construction below -- the whole point of pre-flight is
    # validating what will actually be applied to production, not a
    # close-enough approximation. (.strip() the original FIRST, same order
    # patch_deployment uses -- strips only at the very end would risk a
    # double space in the middle if orig_java_opts had trailing whitespace;
    # cosmetic only, JAVA_OPTS parsing tolerates it, but kept identical on
    # principle since these two are supposed to match exactly.)
    patched_java_opts = orig_java_opts
    if "-javaagent" not in orig_java_opts:
        patched_java_opts = f"{orig_java_opts.strip()} -javaagent:/agent/leak-agent.jar".strip()

    run(["kubectl", "delete", "configmap", CONFIGMAP_NAME, "-n", NAMESPACE, "--ignore-not-found=true"])
    cm = run(["kubectl", "create", "configmap", CONFIGMAP_NAME, "-n", NAMESPACE,
              f"--from-file=leak-agent.jar={LOCAL_JAR}"])
    if cm.returncode != 0:
        print(f"ABORT: could not create the pre-flight ConfigMap: {cm.stderr}")
        sys.exit(1)

    pod_manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": PREFLIGHT_NAME, "namespace": NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "initContainers": [{
                "name": "agent-jar-copy", "image": shipping_image,
                "command": ["sh", "-c", "cp /agent-src/leak-agent.jar /agent/leak-agent.jar"],
                "volumeMounts": [
                    {"name": "agent-src", "mountPath": "/agent-src"},
                    {"name": "agent-jar", "mountPath": "/agent"},
                ],
            }],
            "containers": [{
                "name": CONTAINER, "image": shipping_image,
                "env": [{"name": "JAVA_OPTS", "value": patched_java_opts}],
                "volumeMounts": [
                    {"name": "agent-jar", "mountPath": "/agent"},
                    {"name": "agent-ctl", "mountPath": "/agent-ctl"},
                ],
            }],
            "volumes": [
                {"name": "agent-src", "configMap": {"name": CONFIGMAP_NAME}},
                {"name": "agent-jar", "emptyDir": {}},
                {"name": "agent-ctl", "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"}},
            ],
        },
    }
    apply = subprocess.run(["kubectl", "apply", "-n", NAMESPACE, "-f", "-"],
                            input=json.dumps(pod_manifest), text=True, capture_output=True)
    if apply.returncode != 0:
        print(f"ABORT: could not apply the pre-flight pod: {apply.stderr}")
        sys.exit(1)

    ready = run(["kubectl", "wait", "--for=condition=Ready", f"pod/{PREFLIGHT_NAME}",
                 "-n", NAMESPACE, "--timeout=180s"])
    if ready.returncode != 0:
        print("ABORT: pre-flight pod never became Ready -- inspect it before proceeding "
              f"(`kubectl describe pod {PREFLIGHT_NAME} -n {NAMESPACE}`). Not touching the "
              "real Deployment.")
        run(["kubectl", "delete", "pod", PREFLIGHT_NAME, "-n", NAMESPACE, "--ignore-not-found=true", "--wait=false"])
        sys.exit(1)

    print("  pre-flight pod Ready -- confirming the agent actually loaded (status+beat files)...")
    loaded = False
    for _ in range(10):
        status = kubectl_exec(PREFLIGHT_NAME, CONTAINER, ["sh", "-c", "cat /agent-ctl/status 2>/dev/null"])
        beat = kubectl_exec(PREFLIGHT_NAME, CONTAINER, ["sh", "-c", "cat /agent-ctl/beat 2>/dev/null"])
        if status.stdout.strip() and beat.stdout.strip():
            loaded = True
            break
        time.sleep(2)
    if not loaded:
        print("ABORT: the agent's status/beat files never appeared on the pre-flight pod within "
              "20s -- the jar or JAVA_OPTS patch is likely bad. Not touching the real Deployment. "
              f"Inspect: `kubectl logs {PREFLIGHT_NAME} -n {NAMESPACE} -c {CONTAINER}`.")
        run(["kubectl", "delete", "pod", PREFLIGHT_NAME, "-n", NAMESPACE, "--ignore-not-found=true", "--wait=false"])
        sys.exit(1)
    print("  agent confirmed loaded. Testing a real ALLOCATE/RELEASE round-trip...")

    kubectl_exec(PREFLIGHT_NAME, CONTAINER, [
        "sh", "-c", "printf '%s\\n' 'ALLOCATE 10 ttl=30' > /agent-ctl/cmd.tmp && mv /agent-ctl/cmd.tmp /agent-ctl/cmd",
    ])
    time.sleep(3)
    allocated = kubectl_exec(PREFLIGHT_NAME, CONTAINER, ["sh", "-c", "cat /agent-ctl/status 2>/dev/null"])
    kubectl_exec(PREFLIGHT_NAME, CONTAINER, [
        "sh", "-c", "printf '%s\\n' 'RELEASE' > /agent-ctl/cmd.tmp && mv /agent-ctl/cmd.tmp /agent-ctl/cmd",
    ])
    time.sleep(3)
    if "state=ALLOCATING" not in allocated.stdout and "state=ALLOCATED" not in allocated.stdout \
            and "state=GOVERNED_HOLD" not in allocated.stdout:
        print(f"ABORT: sent a real 10MB ALLOCATE on the pre-flight pod, but its status never showed "
              f"a real allocating/allocated state (got: {allocated.stdout!r}). Not touching the real "
              f"Deployment.")
        run(["kubectl", "delete", "pod", PREFLIGHT_NAME, "-n", NAMESPACE, "--ignore-not-found=true", "--wait=false"])
        sys.exit(1)
    print("  real ALLOCATE/RELEASE round-trip confirmed working.")

    run(["kubectl", "delete", "pod", PREFLIGHT_NAME, "-n", NAMESPACE, "--ignore-not-found=true", "--wait=false"])
    print("=== PRE-FLIGHT PASSED ===\n")


def backup_current_spec() -> Path:
    """Real, exact rollback path -- the full live Deployment spec, saved
    BEFORE any patch, timestamped so a re-run never silently overwrites an
    earlier backup. Rollback re-applies this file verbatim."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"shipping_deployment_pre_install_{ts}.json"
    result = run(["kubectl", "get", "deployment", TARGET, "-n", NAMESPACE, "-o", "json"])
    if result.returncode != 0 or not result.stdout.strip():
        print("ABORT: could not read shipping's real current Deployment spec -- refusing to "
              "proceed without a real rollback backup.")
        sys.exit(1)
    backup_path.write_text(result.stdout)
    print(f"  real pre-install spec backed up to: {backup_path}")
    return backup_path


def patch_deployment() -> None:
    """Real, permanent patch to shipping's live Deployment. Locked
    decisions applied exactly, not approximated:
    - JAVA_OPTS gains ONLY -javaagent:... -- NO GC-logging flags (locked
      decision: real diagnosis signal is heap_used via Prometheus, not GC
      log parsing; permanent unrotated verbose logging is a real,
      avoidable disk risk for zero benefit). NO -Xmx/-Xms change --
      production's real 128m/64m heap stays untouched, unlike the clone
      script's own optional bump.
    - Same real agent-jar/agent-ctl volumes + initContainer the clone
      validated end-to-end.
    - Rollout safety (locked): minReadySeconds=400, maxSurge=1,
      maxUnavailable=0 -- avoids a real outage window on this
      single-replica Deployment during the one-time rollout this patch
      itself triggers.
    """
    get_result = run(["kubectl", "get", "deployment", TARGET, "-n", NAMESPACE, "-o", "json"])
    if get_result.returncode != 0:
        print("ABORT: could not read shipping's live spec to build the patch.")
        sys.exit(1)
    d = json.loads(get_result.stdout)
    spec = d["spec"]["template"]["spec"]
    strategy = d["spec"].setdefault("strategy", {})
    containers = spec["containers"]
    main = next((c for c in containers if c["name"] == CONTAINER), None)
    if main is None:
        print(f"ABORT: no container named {CONTAINER!r} found in shipping's live spec.")
        sys.exit(1)

    java_opts_env = next((e for e in main.get("env", []) if e["name"] == "JAVA_OPTS"), None)
    if java_opts_env is None:
        print("ABORT: shipping's live spec has no JAVA_OPTS env var -- this installer's patch "
              "logic assumes one exists (per the real spec read at design time). Refusing to "
              "guess a new one; inspect the live Deployment manually before proceeding.")
        sys.exit(1)
    orig_java_opts = java_opts_env["value"]
    if "-javaagent" in orig_java_opts:
        print(f"  JAVA_OPTS already has a -javaagent flag ({orig_java_opts!r}) -- leaving it "
              f"untouched. If this isn't our agent, stop and investigate before proceeding.")
    else:
        java_opts_env["value"] = f"{orig_java_opts.strip()} -javaagent:/agent/leak-agent.jar".strip()
        print(f"  real original JAVA_OPTS: {orig_java_opts!r}")
        print(f"  patched JAVA_OPTS:        {java_opts_env['value']!r}")

    main.setdefault("volumeMounts", [])
    if not any(vm["name"] == "agent-jar" for vm in main["volumeMounts"]):
        main["volumeMounts"].append({"name": "agent-jar", "mountPath": "/agent"})
    if not any(vm["name"] == "agent-ctl" for vm in main["volumeMounts"]):
        main["volumeMounts"].append({"name": "agent-ctl", "mountPath": "/agent-ctl"})

    spec.setdefault("volumes", [])
    if not any(v["name"] == "agent-src" for v in spec["volumes"]):
        spec["volumes"].append({"name": "agent-src", "configMap": {"name": CONFIGMAP_NAME}})
    if not any(v["name"] == "agent-jar" for v in spec["volumes"]):
        spec["volumes"].append({"name": "agent-jar", "emptyDir": {}})
    if not any(v["name"] == "agent-ctl" for v in spec["volumes"]):
        spec["volumes"].append({"name": "agent-ctl", "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"}})

    spec.setdefault("initContainers", [])
    if not any(ic["name"] == "agent-jar-copy" for ic in spec["initContainers"]):
        spec["initContainers"].append({
            "name": "agent-jar-copy", "image": main["image"],
            "command": ["sh", "-c", "cp /agent-src/leak-agent.jar /agent/leak-agent.jar"],
            "volumeMounts": [
                {"name": "agent-src", "mountPath": "/agent-src"},
                {"name": "agent-jar", "mountPath": "/agent"},
            ],
        })

    d["spec"]["minReadySeconds"] = MIN_READY_SECONDS
    strategy["type"] = "RollingUpdate"
    strategy["rollingUpdate"] = {"maxSurge": 1, "maxUnavailable": 0}

    for f in ("resourceVersion", "uid", "creationTimestamp", "generation"):
        d["metadata"].pop(f, None)
    d.pop("status", None)

    print("\n=== Applying the real patch to shipping's live Deployment ===")
    apply = subprocess.run(["kubectl", "apply", "-n", NAMESPACE, "-f", "-"],
                            input=json.dumps(d), text=True, capture_output=True)
    if apply.returncode != 0:
        print(f"ABORT: kubectl apply failed: {apply.stderr}")
        sys.exit(1)
    print(f"  {apply.stdout.strip()}")

    # Real bug found and fixed (2026-09-03): the `if "-javaagent" in
    # orig_java_opts` branch above deliberately leaves JAVA_OPTS untouched
    # on a re-run (e.g. rebuilding the jar after a LeakAgent.java change,
    # ConfigMap already refreshed by preflight_validate() above) -- but if
    # nothing ELSE in the pod spec changed either, the `kubectl apply` just
    # above is then a genuine no-op: no new rollout, the existing pod's
    # initContainer never re-runs, so the OLD jar keeps running while this
    # script goes on to report success. wait_and_verify() below then reads
    # the STILL-RUNNING old pod as "the real new pod" -- a real, silent
    # trap, confirmed live (a 3h31m-old pod reported as fresh). Force a
    # rollout restart unconditionally here so a jar-only rebuild always
    # gets a genuinely fresh pod (idempotent/cheap on a real first install,
    # since `kubectl apply` just above already triggers one there too).
    print("=== Forcing a rollout restart (guarantees a fresh pod + fresh initContainer jar copy, "
          "even when JAVA_OPTS itself didn't change on this run) ===")
    restart = run(["kubectl", "rollout", "restart", f"deployment/{TARGET}", "-n", NAMESPACE])
    if restart.returncode != 0:
        print(f"ABORT: kubectl rollout restart failed: {restart.stderr}")
        sys.exit(1)


def wait_and_verify() -> None:
    """Real rollout wait + the same real active-HTTP-readiness poll the
    clone script uses (shipping has no readinessProbe -- `rollout status`
    alone only confirms the container process started, not that Spring
    Boot finished booting), then the same real status+beat confirmation."""
    print("\n=== Waiting for the real rollout to complete ===")
    rollout = run(["kubectl", "rollout", "status", f"deployment/{TARGET}", "-n", NAMESPACE,
                   f"--timeout={MIN_READY_SECONDS + 120}s"])
    if rollout.returncode != 0:
        print(f"ABORT: rollout did not complete cleanly: {rollout.stderr}\n"
              f"The real pre-install backup is still on disk -- run --rollback if this needs reverting.")
        sys.exit(1)

    pod_result = run(["kubectl", "get", "pod", "-n", NAMESPACE, "-l", f"name={TARGET}",
                       "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}"])
    pod = pod_result.stdout.strip()
    pod_ip_result = run(["kubectl", "get", "pod", pod, "-n", NAMESPACE, "-o", "jsonpath={.status.podIP}"])
    pod_ip = pod_ip_result.stdout.strip()
    if not pod or not pod_ip:
        print("ABORT: rollout reported success but no real Running pod/podIP could be resolved.")
        sys.exit(1)
    print(f"  real new pod: {pod} ({pod_ip})")

    # Real check added 2026-09-03, closing the trap the unconditional
    # rollout restart above already guards against structurally -- this is
    # the direct verification, not just relying on "a restart happened."
    # Confirms the in-pod jar the initContainer actually copied matches
    # what was just built, not the stale jar the restart exists to replace.
    local_jar_bytes = Path(LOCAL_JAR).stat().st_size
    in_pod_size = kubectl_exec(pod, CONTAINER, ["wc", "-c", "/agent/leak-agent.jar"])
    in_pod_bytes_str = in_pod_size.stdout.split()[0] if in_pod_size.stdout.split() else None
    if in_pod_bytes_str is None or int(in_pod_bytes_str) != local_jar_bytes:
        print(f"ABORT: in-pod jar size mismatch on the real new pod {pod} "
              f"(expected {local_jar_bytes} bytes from this run's build, "
              f"found {in_pod_bytes_str!r} at /agent/leak-agent.jar). The initContainer did "
              f"not copy the freshly built jar -- do NOT trust this install. Inspect "
              f"`kubectl logs {pod} -n {NAMESPACE} -c agent-jar-copy` before retrying. "
              f"The real pre-install backup is still on disk -- run --rollback if needed.")
        sys.exit(1)
    print(f"  in-pod jar size confirmed matching this run's build ({local_jar_bytes} bytes).")

    front_end_result = run(["kubectl", "get", "pod", "-n", NAMESPACE, "-l", f"name={FRONT_END_LABEL}",
                             "-o", "jsonpath={.items[0].metadata.name}"])
    front_end_pod = front_end_result.stdout.strip()
    if not front_end_pod:
        print("ABORT: no real front-end pod found to run the active readiness probe from.")
        sys.exit(1)

    print("=== Real active wait-for-ready poll (shipping has no readinessProbe) ===")
    waited = 0
    ready = False
    js = (
        "var http = require('http');"
        f"var req = http.request({{hostname:'{pod_ip}', port:80, path:'/shipping', method:'GET', timeout:3000}}, "
        "function(res){process.exit(0);});"
        "req.on('error', function(){process.exit(1);});"
        "req.on('timeout', function(){req.destroy(); process.exit(1);});"
        "req.end();"
    )
    while waited < READY_WAIT_S:
        probe = kubectl_exec(front_end_pod, None, ["node", "-e", js])
        if probe.returncode == 0:
            ready = True
            print(f"  real HTTP response confirmed after {waited}s.")
            break
        time.sleep(5)
        waited += 5
        print(f"  ... still waiting ({waited}s elapsed of {READY_WAIT_S}s max)")
    if not ready:
        print(f"ABORT: no real HTTP response from the new pod after {READY_WAIT_S}s. "
              f"Inspect: `kubectl logs {pod} -n {NAMESPACE} -c {CONTAINER}`. "
              f"The real pre-install backup is still on disk -- run --rollback if needed.")
        sys.exit(1)

    print("=== Confirming the leak agent actually loaded on the real pod ===")
    loaded = False
    for _ in range(10):
        status = kubectl_exec(pod, CONTAINER, ["sh", "-c", "cat /agent-ctl/status 2>/dev/null"])
        beat = kubectl_exec(pod, CONTAINER, ["sh", "-c", "cat /agent-ctl/beat 2>/dev/null"])
        if status.stdout.strip() and beat.stdout.strip():
            loaded = True
            break
        time.sleep(2)
    if not loaded:
        print(f"ABORT: the agent's status/beat files never appeared on the real pod within 20s. "
              f"The app itself is up (HTTP confirmed above), but the agent did not load -- "
              f"inspect `kubectl logs {pod} -n {NAMESPACE} -c {CONTAINER} | grep wardence-leak-agent`. "
              f"The real pre-install backup is still on disk -- run --rollback if needed.")
        sys.exit(1)
    print("  real leak agent confirmed loaded on production shipping.")


def rollback() -> None:
    """Real, exact revert -- re-applies the most recent pre-install backup
    verbatim. Refuses to guess if no backup exists."""
    if not BACKUP_DIR.exists():
        print(f"ABORT: no backup directory found at {BACKUP_DIR} -- nothing to roll back to.")
        sys.exit(1)
    backups = sorted(BACKUP_DIR.glob("shipping_deployment_pre_install_*.json"))
    if not backups:
        print(f"ABORT: no pre-install backups found in {BACKUP_DIR} -- nothing to roll back to.")
        sys.exit(1)
    latest = backups[-1]
    print(f"=== Rolling back shipping to the real pre-install spec: {latest} ===")
    spec_text = latest.read_text()
    apply = subprocess.run(["kubectl", "apply", "-n", NAMESPACE, "-f", "-"],
                            input=spec_text, text=True, capture_output=True)
    if apply.returncode != 0:
        print(f"ABORT: rollback apply failed: {apply.stderr}")
        sys.exit(1)
    print(f"  {apply.stdout.strip()}")
    rollout = run(["kubectl", "rollout", "status", f"deployment/{TARGET}", "-n", NAMESPACE, "--timeout=300s"])
    if rollout.returncode != 0:
        print(f"WARNING: rollback applied but rollout status check failed: {rollout.stderr}")
    else:
        print("  rollback rollout completed.")
    run(["kubectl", "delete", "configmap", CONFIGMAP_NAME, "-n", NAMESPACE, "--ignore-not-found=true"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback", action="store_true",
                         help="Revert shipping to its real pre-install spec (the most recent backup).")
    args = parser.parse_args()

    if args.rollback:
        rollback()
        return

    print("=== Real, ONE-TIME production install: memory-leak's LeakAgent -> shipping ===")
    build_jar()
    preflight_validate()
    backup_current_spec()
    patch_deployment()
    wait_and_verify()
    print("\n=== INSTALL COMPLETE ===")
    print("Real, remaining manual follow-ups, not automated by this script:")
    print("  1. Reset memory-leak's earned trust state (mechanism AND diagnosis signal both changed).")
    print("  2. Confirm operator_api.py's live-status readout/timeout config is updated for the new mechanism.")


if __name__ == "__main__":
    main()
