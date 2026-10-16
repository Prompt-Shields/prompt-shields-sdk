# Contributing to Prompt Shields SDK

Thanks for your interest in contributing.

## Before you start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md).
- For security issues follow [SECURITY.md](SECURITY.md) — **never** open a public
  issue for a vulnerability.
- For anything beyond a small fix, open an issue first so we can agree on the
  approach.

## Development setup

Follow the [Quick Start](README.md#quick-start). In short:

```bash
docker compose up -d db                       # PostgreSQL + pgvector
pip install -e packages/collector/[dev]
pip install -e packages/sdk/[dev]
cd packages/db && alembic upgrade head && cd ../..
cp .env.example .env                          # then set your own values
```

## Checks that must pass

```bash
# Unit tests — no database required
PYTHONPATH=packages:packages/collector \
  python3 -m pytest packages/collector/tests/ -v

# SDK tests
PYTHONPATH=packages/sdk python3 -m pytest packages/sdk/tests/ -v

# Integration tests — requires PostgreSQL
PYTHONPATH=packages:packages/collector python3 -m pytest tests/ -v
```

Gateway changes additionally need its own suite:

```bash
cd gateway
npm install
npm run test:gateway    # jest src/
npm run test:plugins    # jest plugins/
```

There is no aggregate `npm test` in `gateway/` — run both suites. They run
offline. Suites that call live vendor APIs (they need network access and a
gitignored `.creds.json` or vendor env vars) are listed in
`gateway/tests/jest/integrationSuites.js`, left out of the default run, and run
with `npm run test:integration`. `gateway/src/tests/regression/` drives the
whole gateway app with a fake `fetch`; add a case there when you port a
provider or endpoint.

### Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`,
with read-only repository permissions; a newer push to the same branch or PR
cancels the run in progress. Its jobs:

| Job | What it runs |
|---|---|
| SDK | Python 3.11, 3.12 and 3.13: `pip install -e "packages/sdk[dev]"`, `ruff check --select E9,F63,F7,F82 packages/sdk` (syntax errors and undefined names only, until the existing findings of the wider rule set are cleaned up), `pytest packages/sdk/tests` |
| Collector + DB migrations | Python 3.11 against a `pgvector/pgvector:pg16` service: checks there is exactly one alembic head and that `alembic upgrade head` reaches it on an empty database, then runs `pytest packages/collector/tests` with `PS_TEST_DATABASE_URL`. The test role `ps_user` is not a superuser, so the row-level security tests bind as they do in production |
| Gateway | Node 20 (as in `gateway/Dockerfile`): `npm ci`, `tsc --noEmit` (with `conf.example.json` copied to `conf.json`), the default offline jest suite, `npm run build` |
| Partner TS SDK | Node 20: typecheck, jest, build in `packages/partner-ts` |

The gateway job leaves out three stale upstream guardrail suites that fail on
`main` (`plugins/default`, `plugins/qualifire`, `plugins/javelin`; tracked in
#69) with `--testPathIgnorePatterns` in the workflow. The tests themselves are
untouched; when you fix one, delete its entry from the workflow's
`known_failing` list. Do not add to that list to get a PR green. The flag
replaces jest.config.js's ignore list rather than adding to it, so the workflow
re-adds `/node_modules/` and the live-credential suites from
`gateway/tests/jest/integrationSuites.js`.

To reproduce the collector job locally, point `PS_TEST_DATABASE_URL` at a
database owned by a non-superuser role with the `vector` and `uuid-ossp`
extensions created.

## Two licenses live in this repository

| Path | License |
|---|---|
| `gateway/` | **MIT** — fork of [Portkey AI Gateway](https://github.com/Portkey-AI/gateway) |
| everything else | **Apache-2.0** |

This matters when you write code:

- **Keep Prompt Shields logic out of upstream files where you can.** Additions
  belong in clearly-ours modules such as `gateway/src/middlewares/ps-telemetry.ts`.
  The smaller the diff against upstream, the cheaper it is to rebase onto a new
  Portkey release.
- **Record any change to an upstream file** in [`gateway/FORK_NOTICE.md`](gateway/FORK_NOTICE.md).
  That file is the record of what we changed and why — an undocumented divergence
  becomes a silent merge conflict later.
- **Do not remove or alter `gateway/LICENSE`.** MIT requires the notice be
  preserved, and Apache-2.0 requires attribution be carried in [NOTICE](NOTICE).
- **Never copy MIT-licensed gateway code into the Apache-2.0 packages** (or the
  reverse) without carrying its notice along.

Contributions to `gateway/` that are genuinely upstream fixes should be sent to
Portkey as well — everyone benefits, and it shrinks our fork.

## Telemetry and privacy

The collector ingests usage metadata about AI systems. When adding a field to a
telemetry payload, state in the pull request what it contains and why it is
needed. Prompt bodies, message contents, and end-user identifiers must not be
sent to the collector — hashes and metadata only.

## Database migrations

Schema changes go through Alembic in `packages/db`:

```bash
cd packages/db
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
cd ../..
```

Review the generated migration before committing — autogenerate is unreliable
around indexes, constraints, and pgvector columns.

## Coding conventions

- Python 3.11+, typed. Async SQLAlchemy in the collector.
- Public SDK surface changes need a [CHANGELOG.md](CHANGELOG.md) entry.
- Documentation lives in `docs/` as MDX and is the contract the API promises —
  update it in the same pull request as the behaviour change.
- Match the surrounding style rather than reformatting untouched code.

## Never commit

- `.env`, real API keys, database URLs with real hosts, or customer data
- Local research notes and working documents
- `.worktrees/` or build output

## Pull requests

1. Branch from `main` with a descriptive name (`fix/collector-dedup-window`).
2. One concern per pull request.
3. Imperative commit subjects; explain *why* in the body.
4. Say which suites you ran.

By contributing, you agree that your contributions are licensed under the
Apache License 2.0 — except contributions to `gateway/`, which remain under the
MIT License of that fork.
