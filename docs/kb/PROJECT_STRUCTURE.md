# Project structure

CORE is the integration, implemented properly. STUB is the monolith side, thin and marked as such. Dependency rules between the packages are enforced by import-linter (`pyproject.toml`, `make imports`).

```
.
├── README.md                              overview, how to run, decisions, assumptions, gaps (in Russian)
├── CLAUDE.md, .claude/rules/              instructions for the coding agent: invariants, conventions, path-scoped rules
├── pyproject.toml, uv.lock                dependencies, ruff, mypy, pytest, import contracts
├── Makefile                               check / test / up / demo / ...
├── Dockerfile                             target `app` (default) and `fake-provider` (demo only)
├── docker-compose.yml                     Postgres; with profile `app`: migrate, fake provider, the integration
├── .env.example                           local placeholders; application code never reads `.env`
├── .github/workflows/ci.yml               lint, types, import contracts, tests against Postgres
├── config/settings/{base,test}.py         env-driven Django settings
│
├── apps/
│   ├── betting/                           STUB monolith: Bet; place_bet, accept_bet, reject_bet, void_bet;
│   │                                      management command place_demo_bet
│   ├── wallets/                           STUB monolith: Wallet, WalletHold (one per bet); hold, capture, release
│   └── risk_integration/                  CORE
│       ├── api.py                         the only module the monolith imports: enqueue_ticket, TicketSnapshot
│       ├── ports.py                       BetDecisionPort: accept / decline / void
│       ├── adapters/monolith.py           the port over betting and wallets; the only importer of either
│       ├── services.py                    sync use cases run by run_db: lock the ticket, decide through the port,
│       │                                  transition
│       ├── models.py, migrations/         RiskTicket: state, deadlines, lease, payload snapshot, partial indexes
│       ├── domain/                        pure, standard library only: states, transition table, errors
│       ├── protocol/                      pydantic only, every wire-format guess: envelope, messages, codec,
│       │                                  close-code and refused-upgrade classification
│       ├── persistence/                   the ORM: tickets.py (claims with SKIP LOCKED and a lease, conditional
│       │                                  transitions, re-send queries), db.py (DbRunner / run_db),
│       │                                  errors.py (transient / permanent), schema.py (pending migrations)
│       ├── transport/ws_client.py         one provider session over websockets; token provider (stub)
│       ├── runtime/                       async, no ORM
│       │   ├── app.py                     composition root, startup gate, supervision
│       │   ├── connection.py              reconnect loop, the session group (reader, relay, watch), the drain
│       │   ├── relay.py                   the only sender: outbox, retransmission, re-send at session start
│       │   ├── inbound.py                 reader → bounded queue → appliers
│       │   ├── decisions.py               use cases as the loops call them: classify, log, count failures
│       │   ├── sweeper.py                 deadlines → compensation + CANCEL_PENDING
│       │   ├── backoff.py                 reconnect delay per session outcome
│       │   ├── health.py                  heartbeats, startup / liveness / readiness, probe and metrics server
│       │   ├── shutdown.py, supervisor.py SIGTERM request, Drained; TaskGroup where a returning loop is a failure
│       │   ├── config.py                  RISK_* settings (pydantic-settings)
│       │   └── logs.py, metrics.py        JSON logs on stdout; Prometheus metrics
│       ├── management/commands/run_risk_provider.py   the process entry point
│       └── tests/                         unit (domain, protocol, runtime parts) and Postgres (persistence)
│
├── tests/                                 across the monolith and the integration: bet flow, concurrency,
│                                          runtime scenarios against the fake provider, shutdown, the process
├── tools/fake_provider/                   test tooling, not production code: in-process provider with knobs;
│                                          `python -m tools.fake_provider` for compose
│
└── docs/
    ├── PROVIDER_QUESTIONS.md              questions to the provider (A-xx), the business (B-xx) and the tech lead (E-xx)
    └── kb/                                engineering notes, mostly for the coding agent
        ├── DECISIONS.md                   D1-D13: decisions with reasoning and alternatives
        ├── TICKET_LIFECYCLE.md            states and the transition table
        ├── GOTCHAS.md                     library and platform pitfalls met along the way
        ├── PROJECT_STRUCTURE.md           this file
        └── private/                       gitignored
```
