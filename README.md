# Wardence

Wardence runs a real, small microservices app ([Sock Shop](https://github.com/microservices-demo/microservices-demo)) on a local Kubernetes cluster and deliberately breaks it, over and over, with known fault types (crash-loops, out-of-memory, disk exhaustion, and more). Because the failure is injected on purpose, the correct diagnosis is always known — a clean, free source of ground truth that real production incidents never provide.

An AI agent watches the resulting metrics and logs, diagnoses what broke, and — once it's proven itself — is allowed to fix it. A scorer compares every diagnosis and fix outcome against the known truth and keeps a running, per-fault-type record.

**The core idea:** the agent doesn't get blanket permission to act just because it's an LLM. It earns the right to fix a specific type of failure only after a run of verified-correct diagnoses on that exact type, and loses that trust automatically the moment it gets one wrong. For genuinely hard failure types, the system is built to *correctly refuse* to trust itself — and that refusal is treated as a working result, not a gap.

Most existing tools in this space either just investigate and report, or let an AI act under a fixed, human-set policy that's never re-checked. This project is a running implementation of *continuously measured, per-fault-type, revocable* autonomy — a governance model that (as of this writing) exists in industry whitepapers but not, as far as could be found, as a working reference system.

## Status

Actively in development. Current phase: **P2 complete** (read-only diagnosis loop, validated over 20 real fault-injection episodes) — **P3** (giving the agent real, tightly caged write access, plus the trust-earning state machine) is next.

## Stack

Kubernetes (k3s), [Chaos Mesh](https://chaos-mesh.org/) for fault injection, Prometheus/Grafana for telemetry, Python + FastAPI for the agent, SQLite for local ground-truth/scoring, with a cloud LLM backend for the agent's reasoning.

## Layout

```
p2_readonly_loop/
  injector.py      # triggers a real crash-loop, records ground truth
  agent.py         # FastAPI diagnosis service (read-only, no fix capability yet)
  scorer.py        # compares diagnosis to ground truth, logs the verdict
  run_episodes.py  # runs the injector -> agent -> scorer loop N times
  requirements.txt
```

This is a solo portfolio project, not (yet) accepting contributions.
