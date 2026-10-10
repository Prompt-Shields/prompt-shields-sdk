# QA

## Environment (once per tick)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e "packages/sdk[dev]" -e "packages/collector[dev]"
docker compose up -d db 2>/dev/null && \
  until docker compose exec -T db pg_isready -q; do sleep 1; done && \
  (cd packages/db && PYTHONPATH=../../packages alembic upgrade head)
(cd gateway && npm ci)
```

Run this only on `main` or on a trusted PR head (SKILL.md rule 10) — never on
an untrusted fork's code. Record in the journal which parts came up. If Docker is unavailable, the
integration suite is skipped; see `LEARNINGS.md` for what that blocks.

## Suites

| Area touched | Command |
|---|---|
| `packages/sdk/**` | `PYTHONPATH=packages/sdk python3 -m pytest packages/sdk/tests/ -q` |
| `packages/collector/**`, `packages/db/**` | `PYTHONPATH=packages:packages/collector python3 -m pytest packages/collector/tests/ -q`, `PYTHONPATH=packages python3 -m pytest packages/db/ -q`, and the integration suite |
| integration (needs DB) | `PYTHONPATH=packages:packages/collector python3 -m pytest tests/ -q` |
| `gateway/**` | `cd gateway && npm run test:gateway && npm run test:plugins && npm run format:check` |
| docs only | none, but check links and code samples still match the code |

"Required suites" for a PR = every row whose area the PR touches, plus the SDK
suite always (it is fast and catches packaging breakage).

## Post-merge QA

After each merge, on the new `main`:

1. Run the required suites for the merged PR again on `main` (catches bad
   interactions between PRs merged in the same window).
2. If red: stop merging, open a fix PR or a revert PR labelled `autopilot` +
   `regression`, and mention it first in the journal.

## Post-release QA

After a new `sdk-v*` tag:

1. Build and install the tagged SDK into a clean venv, outside the repo:
   ```bash
   python3 -m venv /tmp/ps-qa && /tmp/ps-qa/bin/pip install build
   /tmp/ps-qa/bin/python -m build packages/sdk --outdir /tmp/ps-qa/dist
   /tmp/ps-qa/bin/pip install /tmp/ps-qa/dist/*.whl
   /tmp/ps-qa/bin/python -c "import prompt_shields, sys; print(prompt_shields.__name__)"
   ```
2. Confirm `prompt_shields.__version__` and the installed distribution
   version (`importlib.metadata.version("prompt-shields")`) both match the tag.
3. Run the README quickstart end to end if Docker is available.
4. Spot-check one example per public client in `SDK_GUIDE.md` against the
   installed wheel (import and construct; no real API keys).

Any failure: open a `regression` + `priority:high` issue with the exact
command and output. Never label it `autopilot:ready` yourself (SKILL.md rule 8);
say in the issue whether the fix looks obvious.

## Hourly smoke (when nothing else ran QA this tick)

Run the SDK suite and the collector unit suite on `main`. Cheap, and it
catches dependency drift. Journal the result in one line.
