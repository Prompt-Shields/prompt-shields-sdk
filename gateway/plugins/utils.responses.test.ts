/**
 * PS fork (#73): content-part getters/setters for Responses API requests.
 * Kept apart from utils.test.ts, whose describe.only would skip them.
 */
import { PluginContext } from './types';
import { getCurrentContentPart, setCurrentContentPart } from './utils';

describe('createModelResponse', () => {
  const ctx = (request: any, response: any = {}) =>
    ({
      request: { json: request },
      response: { json: response },
      requestType: 'createModelResponse',
    }) as PluginContext;

  test('string input', () => {
    const context = ctx({ input: 'hello' });
    expect(
      getCurrentContentPart(context, 'beforeRequestHook').textArray
    ).toEqual(['hello']);
    const transformed: any = { request: { json: null }, response: {} };
    setCurrentContentPart(context, 'beforeRequestHook', transformed, ['bye']);
    expect(transformed.request.json).toEqual({ input: 'bye' });
  });

  test('item input: the last item, part by part', () => {
    const context = ctx({
      input: [
        { role: 'user', content: 'first' },
        {
          type: 'message',
          role: 'user',
          content: [
            { type: 'input_text', text: 'a' },
            { type: 'input_image', image_url: 'x' },
            { type: 'input_text', text: 'b' },
          ],
        },
      ],
    });
    expect(
      getCurrentContentPart(context, 'beforeRequestHook').textArray
    ).toEqual(['a', '', 'b']);
    const transformed: any = { request: { json: null }, response: {} };
    setCurrentContentPart(context, 'beforeRequestHook', transformed, [
      'A',
      null,
      'B',
    ]);
    expect(transformed.request.json.input[0]).toEqual({
      role: 'user',
      content: 'first',
    });
    expect(transformed.request.json.input[1].content).toEqual([
      { type: 'input_text', text: 'A' },
      { type: 'input_image', image_url: 'x' },
      { type: 'input_text', text: 'B' },
    ]);
  });

  test('function_call_output as the last item', () => {
    const context = ctx({
      input: [{ type: 'function_call_output', call_id: 'c', output: 'r' }],
    });
    expect(
      getCurrentContentPart(context, 'beforeRequestHook').textArray
    ).toEqual(['r']);
  });

  test('output text and refusal parts', () => {
    const response: any = {
      output: [
        { type: 'reasoning', summary: [] },
        {
          type: 'message',
          content: [
            { type: 'output_text', text: 'one' },
            { type: 'refusal', refusal: 'no' },
          ],
        },
        { type: 'message', content: [{ type: 'output_text', text: 'two' }] },
      ],
      output_text: 'onetwo',
    };
    const context = ctx({ input: 'q' }, response);
    expect(
      getCurrentContentPart(context, 'afterRequestHook').textArray
    ).toEqual(['one', 'no', 'two']);
    const transformed: any = { request: {}, response: { json: null } };
    setCurrentContentPart(context, 'afterRequestHook', transformed, [
      'ONE',
      'NO',
      'TWO',
    ]);
    const out = transformed.response.json;
    expect(out.output[1].content).toEqual([
      { type: 'output_text', text: 'ONE' },
      { type: 'refusal', refusal: 'NO' },
    ]);
    expect(out.output[2].content[0].text).toBe('TWO');
    expect(out.output_text).toBe('ONETWO');
    expect(response.output[1].content[0].text).toBe('one');
  });
});
