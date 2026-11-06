// utils/env uses top-level await, which ts-jest cannot compile here (#69).
jest.mock('../../../utils/env', () => ({ Environment: () => ({}) }));
jest.mock('../../..', () => ({}));

import {
  GoogleChatCompleteResponseTransform as VertexResponseTransform,
  GoogleChatCompleteStreamChunkTransform as VertexStreamTransform,
} from '../chatComplete';
import {
  GoogleChatCompleteResponseTransform as GeminiResponseTransform,
  GoogleChatCompleteStreamChunkTransform as GeminiStreamTransform,
  transformGeminiFinishReason,
} from '../../google/chatComplete';
import { transformGoogleTools } from '../utils';

// PS fork (#208): Gemini/Vertex correctness fixes re-implemented from the
// Portkey enterprise changelog (2.32: blocked prompts, Model Armor and
// prohibited-image finish reasons; 2.33: search-tool parameters).

const blocked = {
  promptFeedback: {
    blockReason: 'PROHIBITED_CONTENT',
    blockReasonMessage: 'The prompt was blocked.',
  },
  usageMetadata: { promptTokenCount: 12, totalTokenCount: 12 },
  modelVersion: 'gemini-2.5-flash',
};

const headers = new Headers();

describe('blocked prompt (no candidates)', () => {
  it.each([
    ['vertex', VertexResponseTransform],
    ['gemini', GeminiResponseTransform],
  ])(
    '%s: returns one content_filter choice with strict compliance',
    (_name, transform) => {
      const out = (transform as any)(blocked, 200, headers, true);
      expect(out.choices).toHaveLength(1);
      expect(out.choices[0]).toEqual({
        index: 0,
        message: { role: 'assistant', content: 'The prompt was blocked.' },
        finish_reason: 'content_filter',
      });
      expect(out.usage.prompt_tokens).toBe(12);
    }
  );

  it.each([
    ['vertex', VertexResponseTransform],
    ['gemini', GeminiResponseTransform],
  ])(
    '%s: keeps the native reason and promptFeedback without strict compliance',
    (_name, transform) => {
      const out = (transform as any)(
        {
          ...blocked,
          promptFeedback: { blockReason: 'SAFETY', safetyRatings: [] },
        },
        200,
        headers,
        false
      );
      expect(out.choices[0].finish_reason).toBe('SAFETY');
      expect(out.choices[0].message.content).toBe(
        'The prompt was blocked by the provider (SAFETY).'
      );
      expect(out.choices[0].promptFeedback).toEqual({
        blockReason: 'SAFETY',
        safetyRatings: [],
      });
    }
  );

  it('a normal response is unchanged', () => {
    const out = (VertexResponseTransform as any)(
      {
        candidates: [
          { content: { parts: [{ text: 'hi' }] }, finishReason: 'STOP' },
        ],
        usageMetadata: { promptTokenCount: 1, candidatesTokenCount: 1 },
        promptFeedback: { safetyRatings: [] },
      },
      200,
      headers,
      true
    );
    expect(out.choices).toHaveLength(1);
    expect(out.choices[0].message.content).toBe('hi');
    expect(out.choices[0].finish_reason).toBe('stop');
  });

  it('a response that is neither is still an invalid-response error (gemini)', () => {
    const out = (GeminiResponseTransform as any)(
      { foo: 'bar' },
      200,
      headers,
      true
    );
    expect(out.error).toBeDefined();
  });

  it.each([
    ['vertex', VertexStreamTransform],
    ['gemini', GeminiStreamTransform],
  ])('%s stream: the blocked chunk carries a finished choice', (_n, fn) => {
    const line = (fn as any)(
      `data: ${JSON.stringify(blocked)}`,
      'chatcmpl-1',
      {},
      true
    );
    const chunk = JSON.parse(line.replace(/^data: /, ''));
    expect(chunk.choices).toEqual([
      {
        index: 0,
        delta: { role: 'assistant', content: 'The prompt was blocked.' },
        finish_reason: 'content_filter',
      },
    ]);
    expect(chunk.usage.prompt_tokens).toBe(12);
  });
});

describe('content-filter finish reasons', () => {
  it.each(['MODEL_ARMOR', 'IMAGE_PROHIBITED_CONTENT', 'IMAGE_SAFETY'])(
    '%s maps to content_filter with strict compliance',
    (reason) => {
      expect(transformGeminiFinishReason(reason, true, false)).toBe(
        'content_filter'
      );
      expect(transformGeminiFinishReason(reason, false, false)).toBe(reason);
    }
  );

  it('a Vertex Model Armor stop is a content_filter choice', () => {
    const out = (VertexResponseTransform as any)(
      {
        candidates: [
          {
            content: { parts: [] },
            finishReason: 'MODEL_ARMOR',
            finishMessage: 'Blocked by Model Armor.',
          },
        ],
        usageMetadata: { promptTokenCount: 1, candidatesTokenCount: 0 },
      },
      200,
      headers,
      true
    );
    expect(out.choices[0].finish_reason).toBe('content_filter');
  });
});

describe('googleSearch tool parameters', () => {
  it('passes excludeDomains and blockingConfidence through', () => {
    expect(
      transformGoogleTools({
        type: 'function',
        function: {
          name: 'googleSearch',
          parameters: {
            excludeDomains: ['example.com'],
            blockingConfidence: 'BLOCK_LOW_AND_ABOVE',
          },
        },
      } as any)
    ).toEqual([
      {
        googleSearch: {
          excludeDomains: ['example.com'],
          blockingConfidence: 'BLOCK_LOW_AND_ABOVE',
        },
      },
    ]);
  });

  it('sends an empty googleSearch when no parameters are given', () => {
    expect(
      transformGoogleTools({
        type: 'function',
        function: { name: 'google_search' },
      } as any)
    ).toEqual([{ googleSearch: {} }]);
  });
});
