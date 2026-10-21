---
paths:
  - "Dockerfile"
  - "docker-compose.yml"
  - "apps/risk_integration/runtime/health.py"
  - "apps/risk_integration/runtime/metrics.py"
---

# Operability

- Startup probe: settings valid, DB reachable, migrations applied.
- Liveness: heartbeats of the main loops are fresh. It never depends on provider availability or DB health.
- Readiness: "this pod can take over" (initialized, DB reachable). Never "holds the session": that would stall a rolling update. Session status is a metric.
- Probes and metrics are served by one small HTTP server on a separate port.
- Metrics: low-cardinality labels only (state, outcome, reason). Never label by `ticket_id` or `bet_id`. Cover: time to decision (histogram), retransmissions, cancellations by reason, escalations, reconnects by outcome, connection status, applier queue depth, late and unexpected messages. Database-wide gauges (tickets by state, oldest unresolved ticket, outbox depth) belong in a SQL exporter, not in every pod (stage 5 review).
- Kubernetes: `replicas: 1`, RollingUpdate with `maxSurge: 1` and `maxUnavailable: 0`, resource requests and limits, `terminationGracePeriodSeconds` greater than the drain budget plus the WebSocket close timeout. Reasoning in D6 and D8. No manifests in the repository (dropped at the stage 5 review): the task asks for a Kubernetes-friendly process, so the README documents the wiring (probes, grace period, rollout, resources) and the platform owns the manifests. No PDB (with one replica it blocks node drains). Configuration through env vars and secrets, never baked into the image.
- No leader election (D6): every instance connects, and the provider's session policy decides. A refused instance (`ALREADY_CONNECTED`) retries with a short capped backoff and jitter. Never call this a lock, leader or fencing.
- The SIGTERM handler is installed before slow initialization (Python is PID 1 with an exec-form `CMD`).
