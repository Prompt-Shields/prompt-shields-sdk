jest.mock('jose', () => ({}));

import { plugins } from '../../../plugins';
import {
  RESPONSES_SUPPORTED_CHECKS,
  responsesUnsupportedChecks,
} from './responsesSupport';
import { HookObject, HookType } from './types';

const registered = Object.entries(plugins).flatMap(([source, fns]) =>
  Object.keys(fns).map((fn) => `${source}.${fn}`)
);

const hook = (checks: HookObject['checks']): HookObject => ({
  type: HookType.GUARDRAIL,
  id: 'h',
  eventType: 'beforeRequestHook',
  checks,
});

describe('responsesUnsupportedChecks (allowlist, fails closed)', () => {
  test('every allowlisted check is a registered plugin check', () => {
    for (const id of RESPONSES_SUPPORTED_CHECKS) {
      expect(registered).toContain(id);
    }
  });

  test.each([
    'default.webhook',
    'default.addPrefix',
    'exa.online',
    'lasso.classify',
    'aporia.validateProject',
    'crowdstrike-aidr.guardChatCompletion',
    'qualifire.toolUseQuality',
  ])('%s, which reads only Chat Completions fields, is unsupported', (id) => {
    expect(RESPONSES_SUPPORTED_CHECKS.has(id)).toBe(false);
    expect(
      responsesUnsupportedChecks([[hook([{ id, parameters: {} }])]])
    ).toEqual([id]);
  });

  test('unknown and future checks are unsupported', () => {
    expect(
      responsesUnsupportedChecks([
        [hook([{ id: 'acme.newGuard', parameters: {} }])],
        undefined,
      ])
    ).toEqual(['acme.newGuard']);
  });

  test('allowlisted and disabled checks pass', () => {
    expect(
      responsesUnsupportedChecks([
        [
          hook([
            { id: 'default.contains', parameters: {} },
            { id: 'default.webhook', parameters: {}, is_enabled: false },
          ]),
        ],
        [hook([{ id: 'portkey.pii', parameters: {} }])],
        [],
        undefined,
      ])
    ).toEqual([]);
  });
});
