# Wardence

Wardence runs a real, small microservices app ([Sock Shop](https://github.com/microservices-demo/microservices-demo)) on a Kubernetes cluster and deliberately breaks it, over and over, with known fault types (crash-loops, out-of-memory, disk exhaustion, network partitions, memory leaks, and more). Because the failure is injected on purpose, the correct diagnosis is always known — a clean, free source of ground truth that real production incidents never provide.

An AI agent watches the resulting metrics, logs, and traces, diagnoses what broke, and — once it's proven itself — is allowed to fix it. A scorer compares every diagnosis and fix outcome against the known truth and keeps a running, per-fault-type record.

**The core idea:** the agent doesn't get blanket permission to act just because it's an LLM. It earns the right to fix a specific type of failure only after a run of verified-correct diagnoses on that exact type, and loses that trust automatically the moment it gets one wrong. For genuinely hard failure types, the system is built to *correctly refuse* to trust itself — and that refusal is treated as a working result, not a gap.

Most existing tools in this space either just investigate and report, or let an AI act under a fixed, human-set policy that's never re-checked. This project is a running implementation of *continuously measured, per-fault-type, revocable* autonomy — a governance model that (as far as could be found) exists in industry whitepapers but not as a working reference system.

## Status

**Core loop complete (2026-09-04).** See [MILESTONE.md](MILESTONE.md).

Live demo: https://wardence.vercel.app

- 12 fault classes validated end to end (6 auto-fix, 6 report-only), on both
  x86 (WSL2) and ARM64 (Oracle Cloud).
- Three-dimension trust engine: per-class action autonomy, per-class diagnoser
  mode (rule-based ↔ LLM), and per-class LLM-action trust — each earned on a
  streak of verified-correct outcomes, each revoked automatically on one miss.
- Real LLM wiring (multi-provider fallback chain), a blast-radius RBAC cage with
  a semantic tool-call validator, DL/HMM/SPC log-anomaly detectors, calibration
  metrics, and a public dashboard with a live incident-replay viewer.

Remaining work is polish, hardening, and roadmap depth — the founding thesis
(break a live system, watch autonomy be earned and measured) is running.

## Stack

- **Lab:** k3s, [Chaos Mesh](https://chaos-mesh.org/) for fault injection, an
  episode-runner that records ground truth to local SQLite (out-of-band).
- **Telemetry:** kube-prometheus-stack (Prometheus + Grafana), Loki (+ Promtail)
  for logs, Jaeger for traces, k6 for continuous background traffic.
- **Agent:** Python + FastAPI, a multi-turn ReAct loop with real tool use
  (PromQL queries, log queries, an anomaly-detector call, an action proposer).
- **Model backend:** a pluggable provider abstraction with an episode-scoped
  fallback chain (Cloudflare Workers AI → DeepInfra → Gemini → Groq / OpenRouter),
  provider-aware confidence extraction, and per-provider quota accounting.
- **Safety:** a restricted Kubernetes ServiceAccount (`get/list/patch/scale`
  only — no `delete`/`exec`/`create`), a typed action allowlist with
  server-side dry-run, a semantic tool-call validator in front of the cage, and
  a circuit breaker.
- **Detectors:** a DeepLog-style per-service LSTM on Drain-parsed log templates,
  a categorical HMM and rate-based SPC for low-vocabulary services, benchmarked
  against an Isolation Forest baseline.
- **Data + showcase:** results published to Cloudflare R2; a Vite + React
  dashboard on Vercel (Trust Ladder, incident Replay Viewer, Model Scorecard,
  live Operator console). Auth is real accounts with bcrypt + TOTP for admin.

## Layout

```
p2_readonly_loop/    fault injector, rule-based diagnoser, ReAct agent, scorer, episode runners
p3_trust_action/     trust engine (3 dimensions), RBAC cage + actions, tool-call validator,
                     verifier (durability windows), operator API + auth, R2 publisher
p4_frontend/         Vite + React dashboard (deployed to Vercel)
p5_dl_hardening/     DeepLog / HMM / SPC detectors, fault classifier, detector service
traffic_gen/         k6 load scripts
deploy/              Oracle Cloud (ARM64) deployment scripts and runbook
```

This is a solo portfolio project, not (yet) accepting contributions.
