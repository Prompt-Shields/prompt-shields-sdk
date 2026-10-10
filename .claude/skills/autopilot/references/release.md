# Release

Only the SDK package (`packages/sdk`, PyPI name `prompt-shields`) is released.
Do one step per tick; each step is idempotent, so a later tick resumes.

## Step 1 — Is a release due?

All must hold (values in `config.md`):

- `main` is green (last post-merge QA or hourly smoke passed).
- No open PR labelled `regression`.
- At least `release_min_interval` since the last `sdk-v*` tag (or no tag yet).
- At least `release_min_changes` user-facing bullets under CHANGELOG
  `[Unreleased]` that concern the SDK (see "SDK entries" below).
- No release PR already open (label `autopilot:release`). If one is open, go
  to step 3.
- No open `Release sdk: breaking change needs a version decision` issue. If
  one is open, a human still has to pick the version; do nothing this tick.

### SDK entries

A CHANGELOG bullet concerns the SDK if it names the SDK (`(SDK)`, `ShieldsOpenAI`,
`ShieldsAnthropic`, `prompt_shields`, an SDK client or argument) or links only
to files under `packages/sdk/` or `docs/sdks/`. Bullets about the gateway
(`gateway/`, "Gateway …", routers, middlewares), the collector or the database
are **not** SDK entries; they stay under `[Unreleased]`. When a bullet covers
both, it counts for the SDK and is copied, not moved. `### Documentation`
bullets about SDK docs go into the release section but do not count toward
`release_min_changes` and never drive the bump.

## Step 2 — Open the release PR

1. **Pick the version** from the current one in `packages/sdk/pyproject.toml`,
   following SemVer while the major version is 0:
   - any removal or breaking change → **stop**: do not open a release PR.
     Open an issue titled `Release sdk: breaking change needs a version
     decision` listing the entries, label it `autopilot:needs-human`, and
     skip releases until a human closes it
   - any SDK `### Added` / `### Changed` → minor bump
   - only SDK `### Fixed` → patch bump
2. Branch `autopilot/release-sdk-v<version>`.
3. Bump `version` in `packages/sdk/pyproject.toml` **and** `__version__` in
   `packages/sdk/prompt_shields/__init__.py`. Both must match; post-release QA
   checks the installed `prompt_shields.__version__` against the tag.
4. In `CHANGELOG.md`, move the SDK entries from `[Unreleased]` into a new
   `## SDK [<version>] — <YYYY-MM>` section, matching the existing heading
   style. Leave non-SDK entries under `[Unreleased]`.
5. Run the SDK suite and the post-release QA build steps (`qa.md`) against the
   branch, and paste the output in the PR body.
6. Open the PR titled `release: sdk v<version>` with labels `autopilot`,
   `autopilot:release`, and an `Autopilot-Session: <session link>` line in the
   body. It is high risk by definition.

## Step 3 — Merge the release PR

Normal merge rules from `SKILL.md` phase 3 apply. If `release_requires_human`
is true, wait for `autopilot:merge-ok`. Re-check step 1's conditions just
before merging — `main` may have changed.

## Step 4 — Tag

On the merge commit on `main`:

```bash
git tag -a sdk-v<version> -m "prompt-shields SDK v<version>"
git push origin sdk-v<version>
```

If the push of the tag is refused, journal it as a blocker for a human — do
not retry with different credentials or routes.

Then post the CHANGELOG section for the version as a comment on the journal
issue under `RELEASE sdk-v<version>`, so a human can paste it into a GitHub
Release. Publishing to PyPI happens only if `release_publish` is true and a
publishing workflow exists; autopilot never handles PyPI tokens itself.

## Step 5 — Post-release QA

Run `qa.md` → "Post-release QA" against the tag.
