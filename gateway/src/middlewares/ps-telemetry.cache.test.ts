import { buildCacheEvent } from './ps-telemetry';

describe('buildCacheEvent', () => {
  it('estimates tokens saved on HIT (ceil chars/4)', () => {
    const body = { model: 'm', messages: [{ role: 'user', content: 'x'.repeat(40) }] };
    const ev = buildCacheEvent('HIT', body, 'anthropic');
    expect(ev.cache_status).toBe('HIT');
    expect(ev.vendor).toBe('anthropic');
    expect(ev.est_tokens_saved).toBe(Math.ceil(JSON.stringify(body).length / 4));
  });

  it('reports zero tokens saved on MISS', () => {
    const ev = buildCacheEvent('MISS', { model: 'm' }, 'openai');
    expect(ev.cache_status).toBe('MISS');
    expect(ev.est_tokens_saved).toBe(0);
  });
});
