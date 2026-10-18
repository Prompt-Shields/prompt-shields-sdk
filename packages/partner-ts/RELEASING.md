# Releasing @prompt-shields/partner

The package is published to npm by `.github/workflows/publish-partner-ts.yml` when a `partner-ts-v<version>` tag is pushed. Nobody publishes from a laptop: `publishConfig.provenance` is on, and npm only generates provenance in a supported CI.

## One-time setup (team)

These are not in place yet, so the first release is blocked on them:

1. **npm org.** Create (or get access to) the `prompt-shields` organization on npmjs.com, so the `@prompt-shields` scope exists. Add the people who should be able to publish.
2. **`NPM_TOKEN` secret.** Create an npm token that can publish to `@prompt-shields`: a granular access token with read and write on the scope (or the `@prompt-shields/partner` package once it exists), or a classic Automation token. Add it as the repository secret `NPM_TOKEN` (Settings → Secrets and variables → Actions). The workflow fails with a clear error when it is missing.
3. **Protect `partner-ts-v*` tags.** Anyone who can push a `partner-ts-v*` tag can start a publish, so only release maintainers may create them. This is a repository setting the workflow cannot enforce: Settings → Rules → Rulesets → New ruleset → **New tag ruleset**:
   - Name `partner-ts release tags`, Enforcement status **Active**.
   - Target tags → Add target → Include by pattern → `partner-ts-v*`.
   - Rules: **Restrict creations**, **Restrict updates** and **Restrict deletions** (and **Block force pushes**).
   - Bypass list: the release maintainers (a team or the Maintain/Admin role), so they can still push release tags.

   (Older repositories may show this as Settings → Tags → "Protected tags"; GitHub has replaced that with tag rulesets.) The workflow also checks that the tagged commit is on `main`, so a protected tag can only publish reviewed code.
4. Optional, after the first publish: on npmjs.com, set the package to require two-factor authentication or a token for publishing, and consider an `npm` GitHub environment with required reviewers for the publish job.

## Each release

1. Update the version in **both** `packages/partner-ts/package.json` and the exported `VERSION` in `packages/partner-ts/src/client.ts`. `npm run check:version` (also run in CI) fails if they differ. Run `npm install --package-lock-only` so `package-lock.json` carries the new version too.
2. Move the SDK's entries under `## [Unreleased]` in `CHANGELOG.md` into the release notes as needed.
3. Merge that to `main` through a pull request. CI runs the typecheck, the tests (including the `docs/openapi.yaml` drift check) and the build.
4. Tag the merged commit on `main` and push the tag:

   ```bash
   git checkout main && git pull
   git tag partner-ts-v0.2.0
   git push origin partner-ts-v0.2.0
   ```

5. The workflow then:
   - checks that the tag's version equals `package.json` and `src/client.ts` (`node scripts/check-version.js <tag>`),
   - checks that the tagged commit is on `main`,
   - checks that `NPM_TOKEN` is set,
   - runs `npm ci --ignore-scripts` (no dependency install script runs in the job that holds the id-token and `NPM_TOKEN`), the typecheck, the tests and the build,
   - runs `npm publish --provenance --access public` (with `id-token: write` for provenance).

A failed run publishes nothing. Fix the problem, delete the tag (`git push --delete origin partner-ts-v0.2.0`), and tag again. A version that reached npm cannot be published again, so bump the version instead.

## Versioning

Semantic versioning. While the version is `0.x`, a minor bump may break the API. Adding a field the API already returns is a patch or minor release; renaming or removing a method or type is a breaking change.
