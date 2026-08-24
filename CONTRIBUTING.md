# Contributing

## Development setup

Follow the quickstart in the [README](README.md) to get PostgreSQL, the collector, and the packages installed. For test runs you also need the test database, which `scripts/init-test-db.sql` creates and enables pgvector on when the `db` container is first started.

If you created the database before that script was in place, enable the extension by hand:

```bash
psql -h localhost -U ps_user -d prompt_shields_test -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

## Running the tests

Invoke the three suites one at a time. A bare `pytest`, or any invocation covering more than one of these directories, fails during collection: `tests/`, `packages/sdk/tests/`, and `packages/collector/tests/` are all packages named `tests`, so their `conftest.py` files collide on module name. Passing `--import-mode=importlib` does not help, because the collision comes from the `__init__.py` files rather than the import mode.

```bash
# SDK — no database required
PYTHONPATH=packages/sdk python3 -m pytest packages/sdk/tests -q

# Collector — requires PostgreSQL with pgvector
PYTHONPATH=packages:packages/collector python3 -m pytest packages/collector/tests -q

# End-to-end — requires PostgreSQL with pgvector
PYTHONPATH=packages:packages/collector python3 -m pytest tests -q
```

All three must pass before you open a pull request.

## Database migrations

Schema changes go through Alembic, from `packages/db`:

```bash
cd packages/db
PYTHONPATH=../../packages alembic revision --autogenerate -m "short description"
PYTHONPATH=../../packages alembic upgrade head
PYTHONPATH=../../packages alembic check    # must report no new operations
```

Keep the revision chain unbroken — every revision needs a `down_revision` that resolves — and give each revision a working `downgrade()`. Revision `001` creates the schema on stock PostgreSQL and `002` adds the pgvector column and its HNSW index, so a migration needing pgvector must come after `002`.

## Code conventions

Python targets 3.11 and uses modern type syntax (`str | None`, `list[str]`). The collector is fully async; use `AsyncSession` and `await` throughout rather than mixing in synchronous database calls.

Two rules matter more than style, because the README makes promises about them:

- **Telemetry stays fail-open.** No code path in the SDK may raise, block, or add latency to a caller's LLM call because of a telemetry problem. Failures are logged and swallowed.
- **Content stays on the host by default.** Prompt text is sent only when the caller sets `send_prompt_text=True`; response text is never sent. If you add a field to an event, confirm it carries metadata rather than content.

Every registry query must be scoped by `tenant_id`. Isolation is enforced by predicate, not by the database, so an unscoped query is a cross-tenant data leak.

## Pull requests

Keep changes focused, and explain what you changed and why rather than restating the diff. Add tests for new behaviour, and update the [changelog](CHANGELOG.md) under `Unreleased`.

If your change alters what the SDK collects or transmits, what the collector stores, or any of the boundaries documented in the README's "What this does not do" section, update that section and [SECURITY.md](SECURITY.md) in the same pull request. Those sections are read as commitments, and a change that quietly widens them is a change to the product's security posture.

## The vendored gateway

`gateway/` is a fork of [Portkey AI Gateway](https://github.com/Portkey-AI/gateway), used under the MIT Licence. Keep Prompt Shields code confined to `gateway/src/middlewares/`, and record any change to upstream files in [`gateway/FORK_NOTICE.md`](gateway/FORK_NOTICE.md) so the fork stays reviewable against upstream. Bugs in unmodified upstream code belong upstream.

## Reporting security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).
