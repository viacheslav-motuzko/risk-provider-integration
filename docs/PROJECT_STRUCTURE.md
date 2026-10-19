# PROJECT_STRUCTURE.md

Planned layout. The user creates the scaffold (Django project, app packages, dependencies, tooling, agent context files) BEFORE the first Claude Code session; Claude Code fills the modules stage by stage (`docs/kb/stages.md`). If the scaffold differs from this file, the repository wins; flag the difference.

Legend: `[scaffold]` created by the user up front; `[S1]`..`[S7]` filled in that stage; CORE implemented properly; STUB thin and honestly marked; OPS operability; PRIVATE gitignored.

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
├── README.md                              [S7]
├── pyproject.toml                         [scaffold] deps, ruff, type checker, pytest, import-linter
├── Makefile                               [scaffold] test / lint / migrate / up
├── docker-compose.yml                     [scaffold: postgres] [S6: + app, fake provider]
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
│   │   ├── services.py                    [S3] place_bet, accept_bet, reject_bet, void_bet
│   │   ├── events.py                      [S3] EventPublisher port + logging implementation
│   │   └── management/commands/
│   │       └── place_demo_bet.py          [S3] demo / load generator
│   │
│   ├── wallets/                           STUB: monolith side
│   │   ├── models.py                      [S3] Wallet, WalletTransaction (HOLD / CAPTURE / RELEASE)
│   │   └── services.py                    [S3] hold, capture, release (idempotent per bet)
│   │
│   └── risk_integration/                  CORE
│       ├── models.py                      [S2] RiskTicket (state, deadline_at, retransmit_at, locked_by, locked_until,
│       │                                  attempts, ...)
│       ├── migrations/                    [S2]
│       ├── api.py                         [S3] the ONLY entry point the monolith uses: enqueue_ticket(...)
│       ├── ports.py                       [S3] BetDecisionPort (accept / decline / void), Clock
│       ├── domain/                        [S2] pure: stdlib only
│       │   ├── states.py, machine.py, errors.py
│       ├── protocol/                      [S1] stdlib + pydantic only
│       │   ├── envelope.py                {type, ticket_id, ts, payload}
│       │   ├── messages.py                outbound: Ticket, ConfirmRecommendation, DeclineRecommendation,
│       │   │                              RequestCancellation; inbound: Recommendation, Error (A-01, A-02)
│       │   ├── codec.py                   encode / decode, malformed and unknown frames
│       │   └── close_codes.py             AUTH_FAILED / DISPLACED / ALREADY_CONNECTED / TRANSIENT
│       ├── persistence/                   [S2] the only ORM users besides models.py
│       │   ├── tickets.py                 create_in_tx, claim_batch (SKIP LOCKED + lease), transition (CAS),
│       │   │                              due_for_retransmit, find_expired, requeue_expired_leases
│       │   └── db.py                      run_db(): semaphore, context copy, SET LOCAL timeouts, hygiene
│       ├── adapters/monolith.py           [S3] DjangoBetDecisionAdapter -> betting / wallets services
│       ├── transport/ws_client.py         [S4] connect, token provider (static stub), keepalive, close classification
│       ├── runtime/                       [S4] async; never touches the ORM directly
│       │   ├── app.py                     composition root (manual wiring)
│       │   ├── config.py, clock.py
│       │   ├── supervisor.py              TaskGroup, fail-fast, shutdown orchestration
│       │   ├── relay.py                   outbox sender (the only sender on the socket), retransmission
│       │   ├── inbound.py                 reader -> bounded queue -> N appliers; one DB transaction per message, CAS
│       │   ├── sweeper.py                 deadlines -> compensation + CANCEL_PENDING; expired leases
│       │   ├── health.py                  [S5] OPS startup / liveness / readiness
│       │   └── metrics.py                 [S5] OPS prometheus metrics
│       ├── management/commands/run_risk_provider.py   [S4] thin: build app, run supervisor
│       └── tests/                         unit (domain, protocol), integration (persistence on Postgres),
│                                          runtime (against the fake provider)
│
├── tools/fake_provider/                   [S4 minimal, S6 scripted] NOT production code: server.py, scenarios.py, __main__.py
├── tests/e2e/                             [S6] scenario tests across processes / compose
├── deploy/k8s/                            [S5] OPS: deployment.yaml (replicas 1, probes, resources, grace period, rollout),
│                                          optional pdb.yaml, servicemonitor.yaml
│
└── docs/
    ├── PROJECT_STRUCTURE.md               this file
    ├── PROVIDER_QUESTIONS.md              [S1] questions to the provider (A-xx) and the business (B-xx), assumed answers
    ├── adr/                               ADR-001..007, each written in the stage where the decision lands
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
