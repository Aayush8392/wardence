# Wardence — Deployment Runbook

Real, reproducible bootstrap order for standing up the cluster from scratch
on new hardware (Oracle A1 or otherwise). Built 2026-08-23 by dumping the
live WSL2 cluster's real current state and diffing it against the upstream
manifest -- every script below is sourced from real, currently-running
values, not buildlog memory. Where something is still genuinely unresolved,
it's flagged as such below, not silently assumed.

This file itself is committed (unlike `wardence_context.md`/`wardence_buildlog.md`)
since it's a real operational runbook, not a private working note.

---

## 0. Prerequisites (not yet done, blocking everything below)

- [ ] **arm64 image rebuild** -- if targeting Oracle A1 (Arm-only), the 6-7
      custom `weaveworksdemos/*` images must be rebuilt for arm64 first (see
      review 60, `p2_readonly_loop`'s Dockerfile work -- not started as of
      this runbook). Infra images (`mongo`, `mysql`, `redis`, `rabbitmq`,
      `openzipkin/zipkin`) already have official arm64 builds, no action
      needed. If instead deploying to an x86 host, skip this step entirely
      -- every image reference below works as-is.
- [ ] k3s installed on the target host, `kubectl` pointed at it.
- [ ] Real secrets available to inject as env/k8s Secrets (not committed
      anywhere): `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
      `CLOUDFLARE_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `DEEPINFRA_API_KEY`,
      plus the R2 credential set currently in `p3_trust_action/.env`
      (gitignored, exists locally -- copy manually, never commit it).

## 1. Base application + observability stack

```bash
kubectl create namespace sock-shop
kubectl apply -n sock-shop -f https://raw.githubusercontent.com/microservices-demo/microservices-demo/master/deploy/kubernetes/complete-demo.yaml
```

Then, in order (each has its own real dependency):

```bash
# RBAC cage for the agent -- no external dependency, safe first
kubectl apply -f p3_trust_action/manifests/rbac.yaml

# Prometheus/Grafana (kube-prometheus-stack via Helm) -- remote-write
# receiver MUST be enabled for traffic_gen's k6 metrics to land.
# Confirmed real flag, live-verified 2026-08-23:
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --set prometheus.prometheusSpec.enableRemoteWriteReceiver=true

# Chaos Mesh (needs chaosDaemon.runtime/socketPath set for k3s's containerd path)
helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock

# Loki + Jaeger (P5 hardening stack)
helm install loki grafana/loki -n monitoring -f p5_dl_hardening/manifests/loki-values.yaml
kubectl apply -n monitoring -f p5_dl_hardening/manifests/loki-grafana-datasource.yaml
helm install promtail grafana/promtail -n monitoring -f p5_dl_hardening/manifests/promtail-values.yaml
helm install jaeger jaegertracing/jaeger -n monitoring -f p5_dl_hardening/manifests/jaeger-values.yaml
```

**Not yet verified**: exact Helm repo names/chart versions for the above --
these were the real charts used originally, but re-add the repos
(`helm repo add ...`) and confirm current chart versions still work before
trusting this section blindly; charts drift over time same as everything
else in this project.

## 1a. arm64-only: patch images to the rebuilt GHCR versions (skip on x86)

The upstream `complete-demo.yaml` applied in section 1 pulls the
ORIGINAL x86 `weaveworksdemos/*` images. On an arm64-only host (Oracle
A1), run immediately after section 1, from the repo root:

```bash
bash deploy/patch_arm64_images.sh        # the 7 app-service images
bash deploy/patch_infra_arm64_fixes.sh   # rabbitmq tag bump + catalogue-db/user-db
```

`patch_infra_arm64_fixes.sh` requires `catalogue-db`/`user-db`'s arm64
images to already exist on GHCR -- if they don't yet, run
`deploy/rebuild_arm64_catalogue_db.sh` and `deploy/rebuild_arm64_user_db.sh`
first (from a docker+buildx machine, e.g. WSL2), same pattern as the
original 7 app-service rebuilds.

**Real, non-obvious arm64-specific bugs found and fixed during the
2026-08-23 wardence-prod deployment, worth knowing before repeating this
on a fresh host:**
- `catalogue`'s Deployment hardcodes `command: ["/app"]` -- the real
  published x86 image apparently has its binary at that exact path,
  unlike its GitHub source's Dockerfile (which implies `/app/main`).
  Already fixed at the image level in `rebuild_arm64_catalogue.sh`.
- `mysql:5.7` (catalogue-db's real base) has NO arm64 build at all --
  swapped to `mariadb:10.11`, a verified multi-arch, wire-compatible
  replacement, in `rebuild_arm64_catalogue_db.sh`.
- `rabbitmq:3.6.8-management` predates multi-arch manifest lists --
  bumped to `3.13-management` in `patch_infra_arm64_fixes.sh`.

## 2. Apply every live cluster patch (real values, captured 2026-08-23)

Run from `p2_readonly_loop/`, in this order (a few have real ordering
dependencies -- noted inline):

```bash
# DB fixes -- no dependencies
bash patch_carts_db_mongo_pin.sh
bash patch_orders_db_mongo_pin.sh
bash patch_session_db_disable_rdb.sh

# Observability sidecars/tracing -- needs Prometheus CRDs (ServiceMonitor)
# and Jaeger's real service DNS up first (step 1)
bash patch_catalogue_db_add_mysqld_exporter.sh
bash patch_enable_zipkin_tracing.sh

# Fault-class mechanism prerequisites -- no cross-dependencies, any order
bash patch_queue_master_ephemeral_limit.sh
bash patch_catalogue_and_queue_master_readiness.sh
bash patch_carts_readiness_and_jvm_tuning.sh
bash patch_orders_jvm_tuning.sh
bash patch_rollout_strategy_masking.sh

# arm64-only: user's Mongo-host env var (see section 1a) -- the stock
# manifest names it "mongo", but the real app source only reads
# MONGO_HOST. Needed on x86 too if ever running from-source builds
# instead of the original weaveworksdemos/user image, but only
# discovered via the arm64 rebuild.
bash patch_user_mongo_env.sh

# New, permanent second deployment (crash-loop's warm-standby demo fix)
bash deploy_carts_warm_standby.sh
```

**Real, arm64-specific memory-limit bump, added 2026-08-23**:
`patch_carts_readiness_and_jvm_tuning.sh` and the new
`patch_orders_jvm_tuning.sh` both raise their deployment's memory limit
to 1Gi (from the original 500Mi, tuned for the tight WSL2/laptop dev
box) -- both OOMKilled repeatedly on wardence-prod even with a `-Xmx`
heap cap already in place, and the Oracle host has real confirmed
headroom (9.2GB free) to not need the squeeze. Re-verify this is still
appropriate if ever deploying to a more memory-constrained host again.

**Real gap, not yet closed**: `patch_catalogue_and_queue_master_readiness.sh`
overlaps with `patch_queue_master_ephemeral_limit.sh` (both touch
`queue-master`'s `resources`/spec) -- both use strategic merge patches so
they shouldn't clobber each other, but this exact combined sequence has
never been run end-to-end on a fresh cluster. Verify `queue-master`'s final
spec has BOTH the `ephemeral-storage: 300Mi` limit AND the readinessProbe
after running both.

## 3. Traffic generator

**Fixed 2026-08-2x**: `traffic_gen/manifest.yaml`'s `OPERATOR_API_URL` used
to be hardcoded to a WSL2-internal IP (`172.24.242.120:8002`), confirmed
non-durable even within the WSL2 environment itself. Since
`operator_api.py` runs as a bare host process (never an in-cluster
Service, section 5) in both WSL2 dev and on `wardence-prod`, there is no
in-cluster DNS name for it in either environment -- the real fix resolves
the node's IP dynamically via the Downward API (`status.hostIP`), which is
the same value in both places (the single node the pod is scheduled on IS
the host `operator_api.py` runs on) and needs no per-environment edit.

**Real prerequisite, either environment**: `operator_api.py` must be
launched with `--host 0.0.0.0` (already baked into `deploy/operator-api.service`
for wardence-prod; for local dev, override the docstring's default
`uvicorn operator_api:app --reload --app-dir p3_trust_action --port 8002`
command with `--host 0.0.0.0` added). Without this, the pod genuinely
cannot reach it -- `baseline.js` fails open in that case (checkout keeps
firing normally), so a misconfiguration here degrades silently, not
loudly. Verify with a real request from inside the cluster, not assumed:
`kubectl exec` a shell in any `sock-shop` pod and `wget -qO- $OPERATOR_API_URL/trust` (needs no auth).

**Real, not-yet-checked risk specific to `wardence-prod`**: Oracle's Ubuntu
image may have OS-level firewall rules (`iptables`/`ufw`) that block
inbound port 8002 even for same-host/pod-originated traffic. If the wget
check above fails with a connection timeout (not "connection refused"),
check `sudo iptables -L -n | grep 8002` / `sudo ufw status` before assuming
a code or networking bug.

```bash
kubectl create configmap wardence-traffic-gen-scripts -n sock-shop \
  --from-file=traffic_gen/baseline.js --from-file=traffic_gen/burst.js --from-file=traffic_gen/run.sh
kubectl apply -f traffic_gen/manifest.yaml
```

## 4. Memory-leak's production mechanism (JVM-attach agent)

```bash
python p2_readonly_loop/install_shipping_leak_agent.py
```

Builds the real jar, pre-flight-validates on a throwaway pod, backs up
`shipping`'s real spec, patches the real Deployment, waits for rollout +
real HTTP/agent confirmation. This script's own 20s post-rollout
confirmation window is known too tight on this project's current hardware
(logged as a false-alarm risk, not yet fixed) -- don't assume a timeout
here means it failed; check the pod directly per the script's own printed
guidance.

**Real, non-obvious startup gaps found deploying this on `wardence-prod`
(2026-08-23), all now fixed/documented so a future from-scratch deploy
doesn't hit them blind:**
- `python-dotenv` was missing from `p3_trust_action/requirements.txt`
  despite `model_backend.py` (imported transitively via `operator_api.py`
  -> `publish_to_r2.py` -> `circuit_breaker.py` -> `react_agent.py`)
  requiring it -- crashed the service on first start. **Fixed**: added
  to `requirements.txt` directly, no manual `pip install` needed going
  forward.
- **A genuinely fresh `wardence.db` is missing tables** -- not just
  `episodes` (`operator_api.py`'s own startup reconciliation assumes the
  base schema already exists, doing an `ALTER TABLE episodes ADD COLUMN
  ...`), but every other table that's only ever lazily created as a side
  effect of some code path actually running (`scores`/`episode_snapshots`
  from `p3_scorer.py`, `comparison_sampling_log` from `p3_agent.py`, and
  others) -- hit for real, twice, as two separate crashes on two separate
  fresh-DB code paths (`wardence_buildlog.md`'s 2026-08-2x sessions).
  **Real, one-time fix needed before ANY fresh deployment's first start**
  (run once, before `operator_api.py`/`p3_agent.py`'s systemd units are
  started for the first time):
  ```bash
  python3 deploy/bootstrap_fresh_db.py
  ```
  (safe, idempotent -- every underlying `ensure_*_table` call is
  `CREATE TABLE IF NOT EXISTS`; calls every known one across the codebase
  in one pass, from the repo root with the venv activated, so a future
  untested code path doesn't surface its own missing table blind.)
- `jwt_secret.txt` doesn't exist until `mint_token.py` is run at least
  once (even just `--help` triggers its auto-generation) -- confirmed
  the real login flow 500s with `RuntimeError: jwt_secret.txt not found`
  until this runs. Run once, before the systemd service's first start:
  ```bash
  cd p3_trust_action && python mint_token.py --help
  ```
- **Real, confirmed-benign finding, not a bug**: `memory-leak`'s
  `LeakAgent` shows a persistent `last_error=... InstanceNotFoundException:
  Tomcat:type=GlobalRequestProcessor,name="http-nio-80"` in its status
  file even once healthy. Verified via a local-attach MBean probe (same
  technique as the earlier `ThreadPoolTimingProbe.java` session) that the
  real MBean genuinely exists and is being read successfully
  (`sync_mbean_unavailable=false`) -- the sticky `last_error` field is
  just never cleared after a one-time boot-race error (agent's `premain`
  attaches before Spring Boot's embedded Tomcat connector finishes
  registering). Don't chase this field if it shows up again on a future
  redeploy; check `sync_mbean_unavailable` instead.

## 5. Backend config for public exposure

**Placement decided 2026-08-23: `operator_api.py` runs as a bare process
on the Oracle host, NOT an in-cluster pod** -- same shape as today's WSL2
setup (reaches the cluster via the existing kubeconfig/SA-token-file
approach in `actions.py`/`verifier.py`, zero new infra to build or
re-validate). This resolves the placement ambiguity that used to block
`PROMETHEUS_URL`'s real value and the secrets-delivery question below.

- **CORS**: `CORS_ORIGINS` env var (done, 2026-08-23 -- `operator_api.py`
  defaults to `http://localhost:5173` when unset, so local dev is
  unaffected). Set to the real Vercel domain once it exists (section 7).
- **Prometheus reachability**: `bash p2_readonly_loop/patch_prometheus_nodeport.sh`
  creates a dedicated NodePort Service (30090 -> 9090) alongside the
  existing ClusterIP service, reachable at `http://localhost:30090` from
  the Oracle host itself (single-node cluster, same node). Then
  `PROMETHEUS_URL` env var (done, 2026-08-23 -- defaults to
  `http://localhost:9090`, unaffected until set) -> set to
  `http://localhost:30090` on the Oracle host.
- **Process supervision**: four systemd unit templates, same
  placeholder convention (user/paths, filled in with real values at
  install time) -- `deploy/operator-api.service` (port 8002),
  `deploy/p3-agent.service` (port 8001, the real diagnosis/action
  endpoint every `/trigger` path calls through -- needed, not optional),
  `deploy/detector-service.service` (port 8010, DL/HMM/SPC anomaly
  fallback -- needed for `oom`/`bad-rollout`/`network-latency`/
  `network-partition`/`cpu-throttling`/`under-provisioned-replicas`, not
  needed for `crash-loop`), `deploy/p2-agent.service` (port 8000, P2's
  legacy standalone agent -- no real pipeline code calls it any more,
  but `run_episodes.py`'s own infra pre-flight check, used by
  `run_batch_plan.py`, does; found missing during `disk-full`
  re-validation). A bare `uvicorn` process has no self-healing on its
  own; these restart it on crash and survive a reboot.
  - `detector-service.service` needs its own Python deps
    (`pip install -r p5_dl_hardening/requirements.txt` -- **install
    torch separately first**, `pip install torch --index-url
    https://download.pytorch.org/whl/cpu`, see that file's own header;
    a plain `pip install torch` pulls the full GPU/CUDA build, wrong on
    a CPU-only host) and its real trained model artifacts
    (`p5_dl_hardening/pipeline_state/` -- gitignored, ~464MB, trained on
    real historical Loki data on the original dev machine, not
    reproducible on a fresh cluster with no log history yet -- `scp -r`
    it over from the dev machine, same as the two `.env` files below).
  - `detector-service.service` also needs Loki reachable via a NodePort
    (`bash p2_readonly_loop/patch_loki_nodeport.sh`, same shape as
    Prometheus' -- exposes it at `http://localhost:30100`, matching
    `LOKI_URL` in the unit template).
  - **Before starting `operator-api.service`/`p3-agent.service` for the
    first time on any fresh DB**: `python3 deploy/bootstrap_fresh_db.py`
    (see the fresh-`wardence.db` gotcha above -- creates every table
    either process' first real request would otherwise hit blind).
- **`--host 0.0.0.0`**: baked into the systemd unit's `ExecStart` above,
  not a separate step.
- **Secrets**: NOT a new mechanism -- confirmed 2026-08-23 that two real
  `.env` files already exist and are already loaded by the Python code
  itself: repo-root `.env` (LLM provider keys: `GEMINI_API_KEY`,
  `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CLOUDFLARE_API_KEY`,
  `CLOUDFLARE_ACCOUNT_ID`, `DEEPINFRA_API_KEY` -- loaded by
  `model_backend.py` via `python-dotenv`) and `p3_trust_action/.env` (R2
  credentials -- loaded by `publish_to_r2.py`'s own `load_env()`, not
  python-dotenv). Both files are gitignored and exist only locally today
  -- they need to be manually copied (never committed) to the same two
  relative locations on the Oracle host with real values. No code change
  needed for this.
- **SA token**: `p3_trust_action/sa_token.txt` (gitignored) needs
  regenerating against the NEW cluster's `wardence-agent` ServiceAccount
  (created by `rbac.yaml` in section 1) -- `kubectl create token
  wardence-agent -n sock-shop --duration=720h > sa_token.txt`, same
  command already used to refresh it locally.
- **JWT secret / admin TOTP**: also need fresh provisioning on the new
  cluster (`mint_token.py` auto-generates `jwt_secret.txt` on first run;
  `create_admin_account.py` walks through TOTP setup) -- not copyable
  from the old cluster, these are meant to be per-deployment.

## 6. Accounts

```bash
cd p3_trust_action
python create_admin_account.py     # bootstraps the first admin (direct DB write)
# then, once operator_api.py is reachable:
python create_demo_trigger_account.py
python create_viewer_account.py
```

## 7. Frontend

Not started. `p4_frontend/.env` still points at localhost. Needs a real
Vercel project + `VITE_R2_BASE_URL`/`VITE_OPERATOR_API_URL`/
`VITE_STOREFRONT_URL` set to real values, plus a CORS domain update on the
backend (section 5) once the real Vercel URL is known.

## 8. Explicitly NOT covered by this runbook, still open

- Fault-class threshold re-validation on new hardware (the "point 6" of
  the deployment checklist) -- every timing-sensitive number in this
  project (network-latency's 200/300ms, memory-leak's burst sizing,
  disk-full's write rate, cpu-throttling's periods delta, UPR's VUS=130
  cliff) was calibrated against the current WSL2/laptop node. Real
  re-calibration pass needed once the new cluster is up, not assumed to
  transfer.
- P6 abuse-prevention (global daily cap, self-registration/CAPTCHA design)
  -- discussed, never built.
- Security review of the now-public-facing Operator API -- never done.
- Real LLM provider quota re-check (Cloudflare Neurons, Gemini's thin
  20 req/day tier, Groq/OpenRouter RPD) -- confirm still valid before
  going live, providers have already changed terms once mid-project.

---

**Provenance note**: every value in the patch scripts referenced above was
pulled from the live cluster's real current spec on 2026-08-23 (via
`p2_readonly_loop/dump_cluster_state.sh`, output gitignored), cross-checked
against each deployment's own `kubectl.kubernetes.io/last-applied-configuration`
annotation to distinguish "original upstream value" from "live-patched
value" where relevant (this is how `orders-db`'s undocumented mongo pin was
found). Not reconstructed from `wardence_buildlog.md` prose alone.
