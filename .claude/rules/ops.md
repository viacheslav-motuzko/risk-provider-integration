---
paths:
  - "Dockerfile"
  - "docker-compose.yml"
  - "apps/risk_integration/runtime/health.py"
  - "apps/risk_integration/runtime/metrics.py"
  - "apps/risk_integration/management/**"
---

# Operability

- Startup: settings valid, DB reachable, migrations applied; no loop runs before. Readiness: started, not draining, a unit of work committed within `RISK_READINESS_DB_WITHIN` (passive, never a `run_db` call per probe). Never "holds the session".
- Probes and `/metrics` share one aiohttp server on its own port, opened before the startup check; it is not a supervised task.
- Metric labels are bounded enums (state, outcome, reason, kind). Database-wide gauges (tickets by state, oldest unresolved ticket) belong in a SQL exporter, not in every pod.
- Prometheus metrics are process-wide: two app instances in one test share them.
- The SIGTERM handler is installed on the event loop before the app's slow start; the image runs Python as PID 1 with an exec-form `CMD`.
- No Kubernetes manifests in the repository: D8 in `docs/kb/decisions.md` documents the wiring (probes, grace period, rollout, resources), and it must stay consistent with the timeouts in `runtime/config.py` (grace period above drain plus close plus the DB await timeout).
- The `app` image carries no tests and no tools; the `fake-provider` target is for the demo only.
