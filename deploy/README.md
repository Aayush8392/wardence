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
bash patch_rollout_strategy_masking.sh

# New, permanent second deployment (crash-loop's warm-standby demo fix)
bash deploy_carts_warm_standby.sh
```

**Real gap, not yet closed**: `patch_catalogue_and_queue_master_readiness.sh`
overlaps with `patch_queue_master_ephemeral_limit.sh` (both touch
`queue-master`'s `resources`/spec) -- both use strategic merge patches so
they shouldn't clobber each other, but this exact combined sequence has
never been run end-to-end on a fresh cluster. Verify `queue-master`'s final
spec has BOTH the `ephemeral-storage: 300Mi` limit AND the readinessProbe
after running both.

## 3. Traffic generator

**Blocking real issue, not yet resolved**: `traffic_gen/manifest.yaml`'s
`OPERATOR_API_URL` is hardcoded to a WSL2-internal IP
(`172.24.242.120:8002`), confirmed non-durable even within the current
environment (documented in the buildlog as a known caveat from the
request-synced GC trigger work). Before deploying anywhere else, this
needs to become the real in-cluster service DNS for `operator_api.py`
(e.g. `wardence-operator-api.sock-shop.svc.cluster.local:8002`, once that
service exists -- see section 5), not another hardcoded IP.

```bash
kubectl create configmap wardence-traffic-gen-scripts -n sock-shop \
  --from-file=traffic_gen/baseline.js --from-file=traffic_gen/burst.js --from-file=traffic_gen/run.sh
kubectl apply -f traffic_gen/manifest.yaml   # after fixing OPERATOR_API_URL above
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
- **Process supervision**: `deploy/operator-api.service` -- a systemd
  unit template (placeholders for user/paths/domain, not yet filled in
  with real values -- needs the real Vercel domain and a real venv path
  first). A bare `uvicorn` process has no self-healing on its own; this
  is what restarts it on crash.
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
