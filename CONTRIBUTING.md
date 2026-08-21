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

There is no aggregate `npm test` in `gateway/` — run both suites.

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
