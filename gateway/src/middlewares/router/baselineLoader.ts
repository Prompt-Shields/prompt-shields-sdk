/**
 * Node-only loader for the savings baseline (kept out of ./baseline.ts so
 * edge bundles never import `fs`).
 */
import {
  Baseline,
  BaselineError,
  parseBaseline,
  setBaseline,
} from './baseline';

/**
 * Node startup: load the `baseline.v1` document named by `PS_BASELINE_FILE`
 * and make it the gateway's baseline. Returns it, or undefined when the
 * variable is unset. Throws `BaselineError` on an unreadable or invalid file.
 */
export async function loadBaselineFromEnv(
  env: Record<string, string | undefined> = process.env
): Promise<Baseline | undefined> {
  const path = env.PS_BASELINE_FILE;
  if (!path) return undefined;
  const { readFile } = await import('fs/promises');
  let text: string;
  try {
    text = await readFile(path, 'utf-8');
  } catch {
    throw new BaselineError(`cannot read ${path}`);
  }
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch {
    throw new BaselineError(`${path} is not valid JSON`);
  }
  const baseline = parseBaseline(json);
  setBaseline(baseline);
  return baseline;
}
