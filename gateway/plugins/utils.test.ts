import { PluginContext } from './types';
import { getCurrentContentPart, setCurrentContentPart } from './utils';

describe('utils', () => {
  describe('getCurrentContentPart', () => {
    test('chatComplete string request content: should return the last message content', () => {
      const context = {
        request: {
          json: {
            messages: [
              { role: 'system', content: 'This is a system message' },
              { role: 'user', content: 'This is a user message' },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toBe('This is a user message');
      expect(result.textArray).toEqual(['This is a user message']);
    });

    test('chatComplete array request content: should return the last message content', () => {
      const context = {
        request: {
          json: {
            messages: [
              { role: 'system', content: 'This is a system message' },
              {
                role: 'user',
                content: [
                  { type: 'text', text: 'This is a user message 1' },
                  { type: 'text', text: 'This is a user message 2' },
                ],
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toEqual([
        { type: 'text', text: 'This is a user message 1' },
        { type: 'text', text: 'This is a user message 2' },
      ]);
      expect(result.textArray).toEqual([
        'This is a user message 1',
        'This is a user message 2',
      ]);
    });

    test('chatComplete string response content: should return the last message content', () => {
      const context = {
        response: {
          json: {
            choices: [
              {
                message: {
                  role: 'assistant',
                  content: 'This is a first response message',
                },
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'afterRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toBe('This is a first response message');
      expect(result.textArray).toEqual(['This is a first response message']);
    });

    test('chatComplete string response content: should return the last message content from multiple choices', () => {
      const context = {
        response: {
          json: {
            choices: [
              {
                message: {
                  role: 'assistant',
                  content: 'This is a first response message',
                },
              },
              {
                message: {
                  role: 'assistant',
                  content: 'This is a second response message',
                },
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'afterRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toBe('This is a second response message');
      expect(result.textArray).toEqual(['This is a second response message']);
    });

    test('complete request content: should return the last message content', () => {
      const context = {
        request: {
          json: {
            prompt: 'This is a user message',
          },
        },
        requestType: 'complete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toEqual('This is a user message');
      expect(result.textArray).toEqual(['This is a user message']);
    });

    test('complete response content: should return the last message content', () => {
      const context = {
        response: {
          json: {
            choices: [
              {
                text: 'This is a first response message',
              },
            ],
          },
        },
        requestType: 'complete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'afterRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toEqual('This is a first response message');
      expect(result.textArray).toEqual(['This is a first response message']);
    });

    test('complete array request content: should return the last message content', () => {
      const context = {
        request: {
          json: {
            prompt: ['This is a user message 1', 'This is a user message 2'],
          },
        },
        requestType: 'complete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook'
      );

      expect(result).toBeDefined();
      expect(result.content).toEqual([
        'This is a user message 1',
        'This is a user message 2',
      ]);
      expect(result.textArray).toEqual([
        'This is a user message 1',
        'This is a user message 2',
      ]);
    });

    test('complete array response content: should return the last message content', () => {
      const context = {
        response: {
          json: {
            choices: [
              {
                text: 'This is a first response message',
              },
              {
                text: 'This is a second response message',
              },
            ],
          },
        },
        requestType: 'complete',
      };
      const result = getCurrentContentPart(
        context as PluginContext,
        'afterRequestHook'
      );
      expect(result).toBeDefined();
      expect(result.content).toEqual('This is a second response message');
      expect(result.textArray).toEqual(['This is a second response message']);
    });
  });

  describe.only('setCurrentContentPart', () => {
    test('chatComplete string request content: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            messages: [
              { role: 'system', content: 'This is a system message' },
              { role: 'user', content: 'This is a user message' },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        ['This is an edited user message']
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        messages: [
          { role: 'system', content: 'This is a system message' },
          { role: 'user', content: 'This is an edited user message' },
        ],
      });
    });

    test('chatComplete array request content: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            messages: [
              { role: 'system', content: 'This is a system message' },
              {
                role: 'user',
                content: [
                  { type: 'text', text: 'This is a user message 1' },
                  { type: 'text', text: 'This is a user message 2' },
                ],
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        ['This is an edited user message 1', 'This is an edited user message 2']
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        messages: [
          { role: 'system', content: 'This is a system message' },
          {
            role: 'user',
            content: [
              { type: 'text', text: 'This is an edited user message 1' },
              { type: 'text', text: 'This is an edited user message 2' },
            ],
          },
        ],
      });
    });

    test('chatComplete array request content: should set the partial current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            messages: [
              { role: 'system', content: 'This is a system message' },
              {
                role: 'user',
                content: [
                  { type: 'text', text: 'This is a user message 1' },
                  { type: 'text', text: 'This is a user message 2' },
                ],
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        [null, 'This is an edited user message 2']
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        messages: [
          { role: 'system', content: 'This is a system message' },
          {
            role: 'user',
            content: [
              { type: 'text', text: 'This is a user message 1' },
              { type: 'text', text: 'This is an edited user message 2' },
            ],
          },
        ],
      });
    });

    test('chatComplete array request content: should not update the partial current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            messages: [
              { role: 'system', content: 'This is a system message' },
              {
                role: 'user',
                content: [
                  { type: 'text', text: 'This is a user message 1' },
                  { type: 'text', text: 'This is a user message 2' },
                ],
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        [null, null]
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        messages: [
          { role: 'system', content: 'This is a system message' },
          {
            role: 'user',
            content: [
              { type: 'text', text: 'This is a user message 1' },
              { type: 'text', text: 'This is a user message 2' },
            ],
          },
        ],
      });
    });

    test('chatComplete string response content: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        response: {
          json: {
            model: 'gpt-4o',
            choices: [
              {
                message: {
                  role: 'assistant',
                  content: 'This is a first response message',
                },
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'afterRequestHook',
        transformedData,
        ['This is an edited response message']
      );
      expect(transformedData.request.json).toBeNull();
      expect(transformedData.response.json).toBeDefined();
      expect(transformedData.response.json).toMatchObject({
        model: 'gpt-4o',
        choices: [
          {
            message: {
              role: 'assistant',
              content: 'This is an edited response message',
            },
          },
        ],
      });
    });

    test('chatComplete string response content: should not set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        response: {
          json: {
            model: 'gpt-4o',
            choices: [
              {
                message: {
                  role: 'assistant',
                  content: 'This is a first response message',
                },
              },
            ],
          },
        },
        requestType: 'chatComplete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'afterRequestHook',
        transformedData,
        [null]
      );
      expect(transformedData.request.json).toBeNull();
      expect(transformedData.response.json).toBeDefined();
      expect(transformedData.response.json).toMatchObject({
        model: 'gpt-4o',
        choices: [
          {
            message: {
              role: 'assistant',
              content: 'This is a first response message',
            },
          },
        ],
      });
    });

    test('complete string request content: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            prompt: 'This is a user message',
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        ['This is an edited user message']
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        prompt: 'This is an edited user message',
      });
    });

    test('complete array request content: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            prompt: ['This is a user message 1', 'This is a user message 2'],
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        ['This is an edited user message 1', 'This is an edited user message 2']
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        prompt: [
          'This is an edited user message 1',
          'This is an edited user message 2',
        ],
      });
    });

    test('complete array request content: should set the partial current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            prompt: ['This is a user message 1', 'This is a user message 2'],
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        ['This is an edited user message 1', null]
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        prompt: [
          'This is an edited user message 1',
          'This is a user message 2',
        ],
      });
    });

    test('complete array request content: should not set the partial current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        request: {
          json: {
            model: 'gpt-4o',
            prompt: ['This is a user message 1', 'This is a user message 2'],
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'beforeRequestHook',
        transformedData,
        [null, null]
      );
      expect(transformedData.request.json).toBeDefined();
      expect(transformedData.response.json).toBeNull();
      expect(transformedData.request.json).toMatchObject({
        model: 'gpt-4o',
        prompt: ['This is a user message 1', 'This is a user message 2'],
      });
    });

    test('complete response content: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        response: {
          json: {
            model: 'gpt-4o',
            choices: [
              {
                text: 'This is a first response message',
              },
            ],
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'afterRequestHook',
        transformedData,
        ['This is an edited first response message']
      );
      expect(transformedData.request.json).toBeNull();
      expect(transformedData.response.json).toBeDefined();
      expect(transformedData.response.json).toMatchObject({
        model: 'gpt-4o',
        choices: [
          {
            text: 'This is an edited first response message',
          },
        ],
      });
    });

    test('complete response multiple choices: should set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        response: {
          json: {
            model: 'gpt-4o',
            choices: [
              {
                text: 'This is a first response message',
              },
              {
                text: 'This is a second response message',
              },
            ],
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'afterRequestHook',
        transformedData,
        ['This is an edited first response message']
      );
      expect(transformedData.request.json).toBeNull();
      expect(transformedData.response.json).toBeDefined();
      expect(transformedData.response.json).toMatchObject({
        model: 'gpt-4o',
        choices: [
          {
            text: 'This is a first response message',
          },
          {
            text: 'This is an edited first response message',
          },
        ],
      });
    });

    test('complete response content: should not set the current content part', () => {
      const transformedData: Record<string, any> = {
        request: {
          json: null,
        },
        response: {
          json: null,
        },
      };
      const context = {
        response: {
          json: {
            model: 'gpt-4o',
            choices: [
              {
                text: 'This is a first response message',
              },
            ],
          },
        },
        requestType: 'complete',
      };
      setCurrentContentPart(
        context as PluginContext,
        'afterRequestHook',
        transformedData,
        [null]
      );
      expect(transformedData.request.json).toBeNull();
      expect(transformedData.response.json).toBeDefined();
      expect(transformedData.response.json).toMatchObject({
        model: 'gpt-4o',
        choices: [
          {
            text: 'This is a first response message',
          },
        ],
      });
    });
  });

  // PS fork (#73)
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
});
