/**
 * gateway/FORK_NOTICE.md "Modifications by Prompt Shields" is a numbered list
 * that CHANGELOG.md and the docs cite by number ("FORK_NOTICE.md item 50").
 * Merge resolutions have produced duplicate numbers before, so check that
 * every item number is unique and that every cited item exists. Gaps are
 * allowed: open PRs reserve numbers.
 */
import { readFileSync } from 'fs';
import { join } from 'path';

const GATEWAY_ROOT = join(__dirname, '..', '..');
const REPO_ROOT = join(GATEWAY_ROOT, '..');
const FORK_NOTICE = join(GATEWAY_ROOT, 'FORK_NOTICE.md');

const CITING_FILES = [
  join(REPO_ROOT, 'CHANGELOG.md'),
  join(GATEWAY_ROOT, 'docs', 'upstream-triage-2026-11.md'),
];

function modificationItemNumbers(): string[] {
  const text = readFileSync(FORK_NOTICE, 'utf8');
  const start = text.indexOf('## Modifications by Prompt Shields');
  expect(start).toBeGreaterThanOrEqual(0);
  const rest = text.slice(start).split('\n').slice(1);
  const end = rest.findIndex((line) => line.startsWith('## '));
  const section = end === -1 ? rest : rest.slice(0, end);
  return section
    .map((line) => /^(\d+[a-z]?)\. /.exec(line)?.[1])
    .filter((n): n is string => n !== undefined);
}

describe('FORK_NOTICE.md modifications list', () => {
  it('has no duplicate item numbers', () => {
    const numbers = modificationItemNumbers();
    expect(numbers.length).toBeGreaterThan(0);
    const seen = new Set<string>();
    const duplicates = numbers.filter((n) => seen.size === seen.add(n).size);
    expect(duplicates).toEqual([]);
  });

  it('has every item that CHANGELOG.md and the docs cite', () => {
    const numbers = new Set(modificationItemNumbers());
    const missing: string[] = [];
    for (const file of CITING_FILES) {
      const text = readFileSync(file, 'utf8');
      for (const m of text.matchAll(/FORK_NOTICE\.md`? items? (\d+[a-z]?)/g)) {
        if (!numbers.has(m[1])) missing.push(`${file}: item ${m[1]}`);
      }
    }
    expect(missing).toEqual([]);
  });
});
