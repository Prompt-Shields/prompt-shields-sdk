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

/** A baseline is a few KB; anything larger is not one. */
export const MAX_BASELINE_FILE_BYTES = 1024 * 1024;

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
  const { readFile, stat } = await import('fs/promises');
  let text: string;
  try {
    const info = await stat(path);
    if (!info.isFile() || info.size > MAX_BASELINE_FILE_BYTES) {
      throw new BaselineError(
        `${path} must be a file of at most ${MAX_BASELINE_FILE_BYTES} bytes`
      );
    }
    text = await readFile(path, 'utf-8');
  } catch (err) {
    if (err instanceof BaselineError) throw err;
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
