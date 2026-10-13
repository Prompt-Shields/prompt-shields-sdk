// utils/env uses top-level await, which ts-jest cannot compile here.
const mockEnv: Record<string, string> = {};
jest.mock('../../utils/env', () => ({ Environment: () => mockEnv }));

import { checkConfigFanOut } from '.';

const leaf = { provider: 'openai' };
const targets = (n: number, t: any = leaf) =>
  Array.from({ length: n }, () => t);

describe('checkConfigFanOut', () => {
  afterEach(() => {
    for (const k of Object.keys(mockEnv)) delete mockEnv[k];
  });

  test('allows configs without targets and within limits', () => {
    expect(checkConfigFanOut({ provider: 'openai' })).toBeNull();
    expect(checkConfigFanOut({ targets: targets(100) })).toBeNull();
  });

  test('rejects more than 100 root targets', () => {
    expect(checkConfigFanOut({ targets: targets(101) })).toContain(
      'limit is 100'
    );
  });

  test('conditional strategy allows up to 1000 root targets', () => {
    const cfg = { strategy: { mode: 'conditional' }, targets: targets(1000) };
    expect(checkConfigFanOut(cfg)).toBeNull();
    cfg.targets = targets(1001);
    expect(checkConfigFanOut(cfg)).toContain('limit is 1000');
  });

  test('rejects more than 50 nested targets under one root target', () => {
    const nested = { targets: targets(5, { targets: targets(10) }) }; // 55
    expect(checkConfigFanOut({ targets: [nested] })).toContain('more than 50');
  });

  test('limits can be overridden by env vars', () => {
    mockEnv.MAX_ROOT_CONFIG_TARGETS = '2';
    expect(checkConfigFanOut({ targets: targets(3) })).toContain('limit is 2');
    mockEnv.MAX_ROOT_CONFIG_TARGETS = 'not-a-number';
    expect(checkConfigFanOut({ targets: targets(3) })).toBeNull();
  });
});
