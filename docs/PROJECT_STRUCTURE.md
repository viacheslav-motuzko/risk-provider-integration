# PROJECT_STRUCTURE.md

Planned layout. The user creates the scaffold (Django project, app packages, dependencies, tooling, agent context files) BEFORE the first Claude Code session; Claude Code fills the modules stage by stage (`docs/kb/stages.md`). If the scaffold differs from this file, the repository wins; flag the difference.

Legend: `[scaffold]` created by the user up front; `[S1]`..`[S6]` filled in that stage; CORE implemented properly; STUB thin and honestly marked; OPS operability; PRIVATE gitignored.

## Layout

```
.
├── CLAUDE.md                              [scaffold] short root instructions
├── CLAUDE.local.md                        [scaffold] PRIVATE: task context and logistics
├── .gitignore                             [scaffold] must list the PRIVATE paths
├── .claude/rules/                         [scaffold] path-scoped rules: domain-protocol, async-runtime,
│                                          persistence, monolith-side, tests, ops
├── .claude/settings.json                  [scaffold] permission allowlist; hook formats Python after each edit
├── .env.example                           [scaffold] placeholders only; `.env` is gitignored (D10)
├── .python-version, uv.lock               [scaffold] Python 3.12; lock committed
├── .dockerignore                          [scaffold]
├── .github/workflows/ci.yml               [scaffold] ruff, mypy, import contracts, pytest with Postgres
├── README.md                              [S6]
├── pyproject.toml                         [scaffold] deps, ruff, type checker, pytest, import-linter
├── Makefile                               [scaffold] test / lint / migrate / up
├── docker-compose.yml                     [scaffold: postgres] [S5: + app, fake provider]
├── Dockerfile                             [S5] OPS, same image as the monolith
├── manage.py                              [scaffold]
│
├── config/                                [scaffold]
│   ├── settings/{base,test}.py            env-driven
│   └── urls.py, wsgi.py
│
├── apps/
│   ├── betting/                           STUB: monolith side
│   │   ├── models.py                      [S3] Bet (PENDING_RISK, ACCEPTED, REJECTED, VOIDED)
│   │   ├── services.py                    [S3] place_bet, accept_bet (typed Rejection), reject_bet, void_bet
│   │   └── management/commands/
│   │       └── place_demo_bet.py          [S3] demo / load generator
│   │
│   ├── wallets/                           STUB: monolith side
│   │   ├── models.py                      [S3] Wallet, WalletHold (one per bet: HELD / CAPTURED / RELEASED)
│   │   ├── services.py                    [S3] hold, capture, release (idempotent per bet); fund (demo, tests)
│   │   └── tests/                         [S3] wallet operations
│   │
│   └── risk_integration/                  CORE
│       ├── models.py                      [S2] RiskTicket (state, deadline_at, retransmit_at, last_sent_at, locked_by,
│       │                                  locked_until, reasons, last error, ...)
│       ├── migrations/                    [S2]
│       ├── api.py                         [S3] the ONLY entry point the monolith uses: enqueue_ticket, TicketSnapshot
│       ├── ports.py                       [S3] BetDecisionPort (accept -> Accepted | Rejected / decline / void)
│       ├── services.py                    [S3] sync use cases run by run_db: apply_recommendation, cancel_ticket
│       │                                  (lock, decide through the port, transition)
│       ├── domain/                        [S2] pure: stdlib only
│       │   ├── states.py, machine.py, errors.py
│       ├── protocol/                      [S1] stdlib + pydantic only
│       │   ├── envelope.py                {type, ticket_id, ts, payload}
│       │   ├── messages.py                outbound: Ticket, ConfirmRecommendation, DeclineRecommendation,
│       │   │                              RequestCancellation; inbound: Recommendation, ProviderError (A-01, A-02)
│       │   ├── codec.py                   encode / decode, malformed and unknown frames
│       │   └── close_codes.py             AUTH_FAILED / DISPLACED / ALREADY_CONNECTED / PLANNED / TRANSIENT
│       ├── persistence/                   [S2] the only ORM users besides models.py
│       │   ├── tickets.py                 create_ticket, claim_due / claim_sent_for_resend / claim_finals_for_resend
│       │   │                              (SKIP LOCKED + lease), release_leases, lock_awaiting, record_* / cancel /
│       │   │                              escalate (CAS), find_expired
│       │   ├── errors.py                  classify_apply_failure: transient / permanent / unclassified (D4)
│       │   └── db.py                      DbRunner (run_db): semaphore, context copy, SET LOCAL timeouts, hygiene
│       ├── adapters/monolith.py           [S3] DjangoBetDecisionAdapter -> betting / wallets services
│       ├── transport/ws_client.py         [S4a] one session: connect, token provider (static stub) fetched with a
│       │                                  timeout, keepalive, library exceptions -> SessionEnded(outcome)
│       ├── runtime/                       [S4a] async; never touches the ORM directly
│       │   ├── app.py                     composition root (manual wiring)
│       │   ├── config.py                  RISK_* settings (pydantic-settings)
│       │   ├── supervisor.py              TaskGroup, fail-fast, a returning loop is a failure
│       │   ├── connection.py              [S4b] reconnect loop around the session group (reader, relay, socket watch)
│       │   ├── backoff.py                 [S4b] reconnect delay per session outcome, streak, jitter
│       │   ├── relay.py                   outbox sender (the only sender on the socket), retransmission, re-send
│       │   │                              at the start of every session, release of its batch when it ends
│       │   ├── inbound.py                 reader -> bounded queue -> N appliers
│       │   ├── decisions.py               use cases through run_db: failure classes, logs, counters
│       │   ├── sweeper.py                 deadlines -> compensation + CANCEL_PENDING
│       │   ├── health.py                  [S5] OPS startup / liveness / readiness
│       │   └── metrics.py                 [S4a counters, S5 served] OPS prometheus metrics
│       ├── management/commands/run_risk_provider.py   [S4a] thin: validate settings, build app, run
│       └── tests/                         unit (domain, protocol), integration (persistence on Postgres),
│                                          runtime (against the fake provider)
│
├── tests/                                 [S3] tests across the monolith and the integration (outside both
│                                          packages, so the import contracts stay strict); [S4a] runtime scenarios
├── tools/fake_provider/                   [S4a] NOT production code, in-process from pytest: server.py (knobs are
│                                          attributes, so no scenarios.py; [S4b] reject-new, token)
│                                          [S5: __main__.py for compose]
├── conftest.py                            [S4a] hides ambient RISK_* variables (make exports .env) from every test
├── deploy/k8s/                            [S5] OPS: deployment.yaml only (replicas 1, probes, resources, grace period, rollout)
│
└── docs/
    ├── PROJECT_STRUCTURE.md               this file
    ├── PROVIDER_QUESTIONS.md              [S1] questions to the provider (A-xx) and the business (B-xx), assumed answers
    ├── DECISIONS.md                       [S6] promoted from kb/decisions.md (no separate ADR files)
    └── kb/                                agent knowledge base, read on demand (index in CLAUDE.md)
        ├── stages.md, decisions.md, provider-assumptions.md, ticket-lifecycle.md,
        │   design-topics.md, gotchas.md
        └── private/                       PRIVATE: TASK.md (original text)
```

## Dependency rules (enforce with import-linter where practical)

- `domain` imports the standard library only.
- `protocol` imports the standard library and pydantic only.
- `persistence` may import Django and `domain`. It is the only package, besides `models.py`, that touches the ORM.
- `runtime` and `transport` are async and must not import the ORM; database access goes through `persistence` via `run_db`.
- `adapters/monolith.py` is the only place in `risk_integration` that imports `betting` or `wallets`.
- `betting` and `wallets` may import `risk_integration.api` only.

Known tension, to discuss at the start of stage 3: `betting.place_bet` must create the ticket in the same transaction as the bet (so it calls `risk_integration.api`), while `risk_integration` applies decisions back into `betting` through the adapter. The coupling is inherent to the monolith; keep it explicit and cheap to cut.

## Planned dependencies

Python 3.12, Django 5.2 LTS, `uv`. Authoritative list: `pyproject.toml`.
Runtime: `django`, `psycopg[binary]` (v3), `dj-database-url`, `websockets` (new asyncio API), `pydantic` (v2), `pydantic-settings`, `prometheus-client`, `aiohttp` (probes and metrics server; revisit in stage 5), `structlog`.
Dev: `pytest`, `pytest-django`, `pytest-asyncio`, `ruff`, `django-stubs[compatible-mypy]` (brings mypy), `import-linter`.
Anything else needs a reason in the stage summary.

## Scaffold checklist for the user

1. Django project `config/` with split settings and `DATABASES` pointing to Postgres via env.
2. Empty app packages `apps/betting`, `apps/wallets`, `apps/risk_integration` with the subpackage directories above.
3. `pyproject.toml` (from the config pack: dependencies, ruff, mypy with django-stubs, pytest, import-linter contracts). Then `uv lock` once and commit `uv.lock`. The contracts name every subpackage under `apps/risk_integration/` (`domain`, `protocol`, `persistence`, `transport`, `runtime`, `adapters`), plus `api.py`, `models.py`, `adapters/monolith.py` and `apps/betting`, `apps/wallets`; `lint-imports` fails if one is missing, so create them with empty `__init__.py`.
4. `Makefile`, `docker-compose.yml`, `Dockerfile` (stub), `.dockerignore`, `.env.example`, `.python-version`, `.github/workflows/ci.yml`, `.claude/settings.json` from the config pack. Copy `.env.example` to `.env` locally.
5. Django settings: `config/settings/base.py` reads `DATABASE_URL` and `DJANGO_SECRET_KEY` from the environment (via `dj-database-url`), `USE_TZ = True`, `TIME_ZONE = "UTC"`; `config/settings/test.py` imports base. `[tool.django-stubs]` and pytest both point at `config.settings.test`.
6. Agent context files in place: `CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/*`, `docs/kb/*`, `docs/kb/private/TASK.md`, this file.
7. `.gitignore` contains `.env`, `CLAUDE.local.md` and `docs/kb/private/`. Run `git status` and confirm both are untracked and ignored BEFORE the first commit. If the repo uses worktrees, copy `CLAUDE.local.md` into each one.
