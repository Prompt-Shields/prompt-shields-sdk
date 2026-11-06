import { TogetherAIChatCompleteStreamChunkTransform } from '../chatComplete';
import { TogetherAICompleteStreamChunkTransform } from '../complete';

// PS fork (#208): keep-alive comments no longer break Together AI streams
// (Portkey enterprise changelog 2.32, re-implemented).
const chatChunk = JSON.stringify({
  id: 'c1',
  object: 'chat.completion.chunk',
  model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
  choices: [{ index: 0, delta: { content: 'hi' }, finish_reason: null }],
});

const chat = (chunk: string) =>
  TogetherAIChatCompleteStreamChunkTransform(chunk, 'fallback', {}, false, {
    model: 'm',
  } as any);

describe('Together AI stream keep-alives (#208)', () => {
  it('skips SSE comment and empty events instead of throwing', () => {
    for (const chunk of [': keep-alive', ':', '  ', 'data: ', ': ping\n: ping'])
      expect(chat(chunk)).toBeUndefined();
    expect(TogetherAICompleteStreamChunkTransform(': keep-alive')).toBe(
      undefined
    );
  });

  it('still transforms data that follows a comment line', () => {
    const out = chat(`: keep-alive\ndata: ${chatChunk}`) as string;
    expect(out.startsWith('data: ')).toBe(true);
    expect(JSON.parse(out.slice(6).trim()).choices[0].delta.content).toBe('hi');
    expect(chat('data: [DONE]')).toBe('data: [DONE]\n\n');
  });

  it('still fails on malformed data', () => {
    expect(() => chat('data: {not json')).toThrow();
  });
});
