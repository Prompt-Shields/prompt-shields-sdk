/**
 * docs/gateway-openapi.yaml against the Hono routes in src/index.ts: every
 * route the app mounts is documented, and every documented operation is
 * mounted. Modelled on packages/collector/tests/test_openapi_spec.py.
 *
 * Routes are read from the source with the TypeScript compiler, so a route
 * mounted only under a runtime condition (the Workers /v1/realtime) counts.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import * as ts from 'typescript';

const GATEWAY_ROOT = join(__dirname, '..', '..');
const INDEX_TS = join(GATEWAY_ROOT, 'src', 'index.ts');
const SPEC_PATH = join(GATEWAY_ROOT, '..', 'docs', 'gateway-openapi.yaml');

const METHODS = ['get', 'put', 'post', 'delete', 'patch'];

// Hono routes with optional segments, expanded to the operations the
// handler actually serves (finetuneHandler picks the endpoint from the
// method and whether :jobId is present).
const EXPANSIONS: Record<string, string[]> = {
  'ALL /v1/fine_tuning/jobs/:jobId?/:cancel?': [
    'GET /v1/fine_tuning/jobs',
    'POST /v1/fine_tuning/jobs',
    'GET /v1/fine_tuning/jobs/{jobId}',
    'POST /v1/fine_tuning/jobs/{jobId}/cancel',
  ],
};

/** Hono path syntax to an OpenAPI path template. */
export function toOpenApiPath(honoPath: string): string {
  const segments = honoPath.split('/');
  return segments
    .map((segment, i) => {
      if (segment === '*') return i === segments.length - 1 ? '{path}' : '{id}';
      // `:name{regex}` and `:name`
      const param = /^:([A-Za-z_][A-Za-z0-9_]*)(\{.*\})?$/.exec(segment);
      if (param) return `{${param[1]}}`;
      return segment;
    })
    .join('/');
}

/** `METHOD /path` for every `app.<method>('<path>', ...)` in src/index.ts. */
function mountedOperations(): Set<string> {
  const source = ts.createSourceFile(
    INDEX_TS,
    readFileSync(INDEX_TS, 'utf8'),
    ts.ScriptTarget.Latest,
    true
  );
  const ops = new Set<string>();
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      ts.isIdentifier(node.expression.expression) &&
      node.expression.expression.text === 'app'
    ) {
      const method = node.expression.name.text;
      const first = node.arguments[0];
      // A path built from a variable or template would be skipped silently.
      if (
        [...METHODS, 'all'].includes(method) &&
        (!first || !ts.isStringLiteralLike(first))
      )
        throw new Error(`app.${method} route needs a string literal path`);
      if (
        [...METHODS, 'all'].includes(method) &&
        first &&
        ts.isStringLiteralLike(first) &&
        first.text !== '*'
      ) {
        const key = `${method.toUpperCase()} ${first.text}`;
        const expanded = EXPANSIONS[key];
        if (expanded) expanded.forEach((op) => ops.add(op));
        else if (method === 'all')
          throw new Error(`app.all route needs an EXPANSIONS entry: ${key}`);
        else ops.add(`${method.toUpperCase()} ${toOpenApiPath(first.text)}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return ops;
}

/**
 * `METHOD /path` for every operation under `paths:` in the spec. The spec is
 * our own file with two-space indentation: path keys at 2 spaces, methods at
 * 4 (a small reader instead of a YAML dependency the gateway does not have).
 */
function documentedOperations(): Set<string> {
  const lines = readFileSync(SPEC_PATH, 'utf8').split('\n');
  const ops = new Set<string>();
  let inPaths = false;
  let currentPath: string | null = null;
  for (const line of lines) {
    if (/^\S/.test(line)) {
      inPaths = line.startsWith('paths:');
      currentPath = null;
      continue;
    }
    if (!inPaths) continue;
    const pathKey = /^ {2}(['"]?)(\/[^'":]*)\1:\s*$/.exec(line);
    if (pathKey) {
      currentPath = pathKey[2];
      continue;
    }
    const methodKey = /^ {4}([a-z]+):\s*$/.exec(line);
    if (currentPath && methodKey && METHODS.includes(methodKey[1])) {
      ops.add(`${methodKey[1].toUpperCase()} ${currentPath}`);
    }
  }
  return ops;
}

const sorted = (s: Set<string>) => [...s].sort();
const minus = (a: Set<string>, b: Set<string>) =>
  sorted(new Set([...a].filter((x) => !b.has(x))));

describe('docs/gateway-openapi.yaml', () => {
  const mounted = mountedOperations();
  const documented = documentedOperations();

  it('finds routes and operations', () => {
    expect(mounted.size).toBeGreaterThan(30);
    expect(documented.size).toBeGreaterThan(30);
    expect(mounted).toContain('POST /v1/chat/completions');
    expect(mounted).toContain('GET /v1/{path}');
    expect(documented).toContain('GET /v1/health');
    // Guards the line reader: one operationId per operation it found.
    const operationIds = readFileSync(SPEC_PATH, 'utf8').match(
      /^ {6}operationId:/gm
    );
    expect(operationIds?.length).toBe(documented.size);
  });

  it('documents every mounted route', () => {
    expect(minus(mounted, documented)).toEqual([]);
  });

  it('mounts every documented operation', () => {
    expect(minus(documented, mounted)).toEqual([]);
  });

  it('converts Hono paths', () => {
    expect(toOpenApiPath('/v1/files/:id/content')).toBe(
      '/v1/files/{id}/content'
    );
    expect(toOpenApiPath('/v1/batches/*/output')).toBe(
      '/v1/batches/{id}/output'
    );
    expect(toOpenApiPath('/v1/proxy/*')).toBe('/v1/proxy/{path}');
    expect(toOpenApiPath('/v1/:path{(?!realtime).*}')).toBe('/v1/{path}');
  });
});
