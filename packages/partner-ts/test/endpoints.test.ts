/**
 * Attestations, risk suggestions, asset modality/lifecycle and the webhook
 * subscription admin API, against a mocked fetch.
 */
import { APIConnectionError, InternalServerError, PartnerClient, PartnerError } from '../src';
import type { AIAsset, AttestationList, RiskMapping, WebhookSubscriptionWithSecret } from '../src';
import { asset, json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const STATUS_WIRE = {
  state: 'attested',
  attested_by: 'ciso@acme.com',
  attested_at: '2026-10-15T09:20:01Z',
  comment: null,
  value_changed: false,
};

const ATTESTATION_WIRE = {
  id: 'att1',
  asset_id: 'id1',
  field: 'data_classification',
  state: 'attested',
  value: 'confidential',
  comment: null,
  attested_by: 'ciso@acme.com',
  attested_at: '2026-10-15T09:20:01Z',
  actor_type: 'partner',
  partner_id: 'p1',
};

const ATTESTATION = {
  id: 'att1',
  assetId: 'id1',
  field: 'data_classification',
  state: 'attested',
  value: 'confidential',
  comment: null,
  attestedBy: 'ciso@acme.com',
  attestedAt: '2026-10-15T09:20:01Z',
  actorType: 'partner',
  partnerId: 'p1',
};

const STATUS = {
  state: 'attested',
  attestedBy: 'ciso@acme.com',
  attestedAt: '2026-10-15T09:20:01Z',
  comment: null,
  valueChanged: false,
};

const SUB_WIRE = {
  id: 's1',
  partner_id: 'p1',
  url: 'https://ea.example.com/hook',
  event_types: ['asset.changed', 'asset.created'],
  description: null,
  is_active: true,
  rotation_grace_ends_at: null,
  created_at: '2026-10-01T00:00:00+00:00',
  updated_at: '2026-10-01T00:00:00+00:00',
};

const SUB = {
  id: 's1',
  partnerId: 'p1',
  url: 'https://ea.example.com/hook',
  eventTypes: ['asset.changed', 'asset.created'],
  description: null,
  isActive: true,
  rotationGraceEndsAt: null,
  createdAt: '2026-10-01T00:00:00+00:00',
  updatedAt: '2026-10-01T00:00:00+00:00',
};

const DELIVERY_WIRE = {
  id: 'd1',
  subscription_id: 's1',
  event_id: 'e1',
  event_type: 'asset.changed',
  asset_id: 'id1',
  status: 'dead',
  attempts: 8,
  last_status_code: 500,
  last_error: 'HTTP 500',
  next_attempt_at: null,
  last_attempt_at: '2026-10-02T00:00:00+00:00',
  delivered_at: null,
  created_at: '2026-10-01T00:00:00+00:00',
};

const path = (url: string) => new URL(url).pathname;
const query = (url: string) => Object.fromEntries(new URL(url).searchParams);

describe('asset fields', () => {
  it('returns modality, region, lifecycle and the attestation map', async () => {
    const m = mockFetch(
      json(
        asset('id1', {
          modality: 'audio',
          region: 'swedencentral',
          lifecycle_state: 'stale',
          lifecycle_changed_at: '2026-10-01T00:00:00Z',
          attestation: { data_classification: STATUS_WIRE, owner: { ...STATUS_WIRE, state: 'unreviewed' } },
        }),
      ),
    );
    const a: AIAsset = await client(m.fetch).assets.get('id1');
    expect(a.modality).toBe('audio');
    expect(a.region).toBe('swedencentral');
    expect(a.lifecycleState).toBe('stale');
    expect(a.lifecycleChangedAt).toBe('2026-10-01T00:00:00Z');
    // Map keys stay as the wire field names; the values are camelCased.
    expect(a.attestation).toEqual({ data_classification: STATUS, owner: { ...STATUS, state: 'unreviewed' } });
  });

  it('filters assets by lifecycle state', async () => {
    const m = mockFetch(json({ data: [], meta: { total: 0, has_more: false, next_cursor: null } }));
    await client(m.fetch).assets.listPage({ lifecycleState: 'retired' });
    expect(query(m.calls[0].url)).toEqual({ lifecycle_state: 'retired' });
  });
});

describe('risks', () => {
  it('passes the status filter and returns suggestion fields', async () => {
    const m = mockFetch(
      json({
        data: [
          {
            id: 'r1',
            risk_category: 'employment',
            risk_level: 'high',
            framework: 'EU_AI_ACT',
            status: 'suggested',
            source: 'rules',
            rule_id: 'eu_ai_act.annex_iii.4a.recruitment',
            ruleset_version: '1.1.0',
            framework_ref: 'Annex III, point 4(a)',
            confidence: 'high',
            evidence: {
              rule_title: 'Recruitment',
              ruleset: 'eu_ai_act_annex_iii',
              summary: 'matched',
              signals: [{ signal: 'use_case', value: 'resume-screening', matched: ['resume*'], role: 'trigger' }],
            },
            reviewed_at: null,
            reviewed_by: null,
          },
        ],
      }),
    );
    const risks: RiskMapping[] = await client(m.fetch).assets.risks('id1', { status: 'suggested' });
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/assets/id1/risks?status=suggested');
    expect(risks[0]).toMatchObject({
      status: 'suggested',
      source: 'rules',
      ruleId: 'eu_ai_act.annex_iii.4a.recruitment',
      rulesetVersion: '1.1.0',
      frameworkRef: 'Annex III, point 4(a)',
      confidence: 'high',
      reviewedAt: null,
      reviewedBy: null,
    });
    expect(risks[0].evidence?.ruleTitle).toBe('Recruitment');
    expect(risks[0].evidence?.signals?.[0].role).toBe('trigger');
  });
});

describe('attestations', () => {
  it('lists the history with field and limit filters', async () => {
    const m = mockFetch(
      json({
        data: [ATTESTATION_WIRE],
        meta: { total: 1, confidence: 'high', current: { data_classification: STATUS_WIRE } },
      }),
    );
    const res: AttestationList = await client(m.fetch).assets.attestations('id1', {
      field: 'data_classification',
      limit: 10,
    });
    expect(path(m.calls[0].url)).toBe('/partner/v1/assets/id1/attestations');
    expect(query(m.calls[0].url)).toEqual({ field: 'data_classification', limit: '10' });
    expect(res).toEqual({
      data: [ATTESTATION],
      meta: { total: 1, confidence: 'high', current: { data_classification: STATUS } },
    });
  });

  it('records an attestation with a snake_case JSON body', async () => {
    const m = mockFetch(
      json({ data: ATTESTATION_WIRE, meta: { confidence: 'high', current: { data_classification: STATUS_WIRE } } }, 201),
    );
    const res = await client(m.fetch).assets.attest('id1', {
      field: 'data_classification',
      state: 'attested',
      attestedBy: 'ciso@acme.com',
      comment: undefined,
    });
    const call = m.calls[0];
    expect(call.method).toBe('POST');
    expect(path(call.url)).toBe('/partner/v1/assets/id1/attestations');
    expect(call.headers['Content-Type']).toBe('application/json');
    expect(call.headers.Authorization).toBe('Bearer k');
    expect(JSON.parse(call.body as string)).toEqual({
      field: 'data_classification',
      state: 'attested',
      attested_by: 'ciso@acme.com',
    });
    expect(res.data).toEqual(ATTESTATION);
    expect(res.meta.current.data_classification).toEqual(STATUS);
  });

  it('does not retry a POST after a 5xx or a network error, which could record it twice', async () => {
    const body = { field: 'owner', state: 'attested', attestedBy: 'a@b.c' };
    const m = mockFetch(json({ title: 'Service Unavailable', status: 503 }, 503));
    await expect(client(m.fetch).assets.attest('id1', body)).rejects.toBeInstanceOf(InternalServerError);
    expect(m.calls).toHaveLength(1);

    const n = mockFetch(new TypeError('socket hang up'));
    await expect(client(n.fetch).assets.attest('id1', body)).rejects.toBeInstanceOf(APIConnectionError);
    expect(n.calls).toHaveLength(1);
  });

  it('still retries a POST after a 429', async () => {
    const m = mockFetch(
      json({ title: 'Too Many Requests', status: 429 }, 429, { 'retry-after': '1' }),
      json({ data: ATTESTATION_WIRE, meta: { confidence: 'high', current: {} } }, 201),
    );
    const res = await client(m.fetch).assets.attest('id1', { field: 'owner', state: 'attested', attestedBy: 'a@b.c' });
    expect(res.data.id).toBe('att1');
    expect(m.calls).toHaveLength(2);
  });

  it('keeps retrying GETs after a 5xx', async () => {
    const m = mockFetch(json({}, 503), json({ data: [], meta: { total: 0, confidence: 'low', current: {} } }));
    await client(m.fetch).assets.attestations('id1');
    expect(m.calls).toHaveLength(2);
  });
});

describe('webhooks (tenant admin API)', () => {
  const SUBS = '/api/v1/admin/webhooks/subscriptions';

  it('creates a subscription and returns the secret once', async () => {
    const m = mockFetch(json({ ...SUB_WIRE, secret: 'whsec_x', warning: 'Save it' }, 201));
    const sub: WebhookSubscriptionWithSecret = await client(m.fetch).webhooks.subscriptions.create({
      partnerId: 'p1',
      url: 'https://ea.example.com/hook',
      eventTypes: ['asset.created', 'asset.changed'],
    });
    expect(m.calls[0].method).toBe('POST');
    expect(path(m.calls[0].url)).toBe(SUBS);
    expect(JSON.parse(m.calls[0].body as string)).toEqual({
      partner_id: 'p1',
      url: 'https://ea.example.com/hook',
      event_types: ['asset.created', 'asset.changed'],
    });
    expect(sub).toEqual({ ...SUB, secret: 'whsec_x', warning: 'Save it' });
  });

  it('lists, gets and deletes subscriptions', async () => {
    const m = mockFetch(json({ data: [SUB_WIRE] }), json(SUB_WIRE), new Response(null, { status: 204 }));
    const c = client(m.fetch);
    expect(await c.webhooks.subscriptions.list({ partnerId: 'p1' })).toEqual([SUB]);
    expect(await c.webhooks.subscriptions.get('s1')).toEqual(SUB);
    await expect(c.webhooks.subscriptions.delete('s1')).resolves.toBeUndefined();
    expect(m.calls.map((x) => [x.method, path(x.url)])).toEqual([
      ['GET', SUBS],
      ['GET', `${SUBS}/s1`],
      ['DELETE', `${SUBS}/s1`],
    ]);
    expect(query(m.calls[0].url)).toEqual({ partner_id: 'p1' });
  });

  it('PATCHes only the fields given', async () => {
    const m = mockFetch(json({ ...SUB_WIRE, is_active: false }), json({ ...SUB_WIRE, url: 'https://new.example.com' }));
    const c = client(m.fetch);
    const paused = await c.webhooks.subscriptions.update('s1', { isActive: false });
    expect(paused.isActive).toBe(false);
    await c.webhooks.subscriptions.update('s1', {
      url: 'https://new.example.com',
      eventTypes: ['asset.retired'],
      isActive: undefined,
    });
    expect(m.calls.map((x) => [x.method, path(x.url), JSON.parse(x.body as string)])).toEqual([
      ['PATCH', `${SUBS}/s1`, { is_active: false }],
      ['PATCH', `${SUBS}/s1`, { url: 'https://new.example.com', event_types: ['asset.retired'] }],
    ]);
    expect(m.calls[0].headers['Content-Type']).toBe('application/json');
  });

  it('refuses an empty PATCH before sending it', () => {
    const m = mockFetch();
    expect(() => client(m.fetch).webhooks.subscriptions.update('s1', {})).toThrow(PartnerError);
    expect(() => client(m.fetch).webhooks.subscriptions.update('s1', { url: undefined })).toThrow(/at least one/);
    expect(m.calls).toHaveLength(0);
  });

  it('rotates the secret, with and without a grace period, and never retries it on 5xx', async () => {
    const rotated = { ...SUB_WIRE, rotation_grace_ends_at: '2026-10-02T00:00:00+00:00', secret: 'whsec_new', warning: 'w' };
    const m = mockFetch(json(rotated), json({ ...rotated, rotation_grace_ends_at: null }), json({}, 502));
    const c = client(m.fetch);
    const res = await c.webhooks.subscriptions.rotateSecret('s1', { gracePeriodSeconds: 86400 });
    expect(res.secret).toBe('whsec_new');
    expect(res.rotationGraceEndsAt).toBe('2026-10-02T00:00:00+00:00');
    await c.webhooks.subscriptions.rotateSecret('s1');
    await expect(c.webhooks.subscriptions.rotateSecret('s1')).rejects.toBeInstanceOf(InternalServerError);
    expect(m.calls.map((x) => [x.method, path(x.url), JSON.parse(x.body as string)])).toEqual([
      ['POST', `${SUBS}/s1/rotate-secret`, { grace_period_seconds: 86400 }],
      ['POST', `${SUBS}/s1/rotate-secret`, {}],
      ['POST', `${SUBS}/s1/rotate-secret`, {}],
    ]);
  });

  it('lists dead letters and redelivers one', async () => {
    const m = mockFetch(json({ data: [DELIVERY_WIRE] }), json({ ...DELIVERY_WIRE, status: 'pending', attempts: 0 }));
    const c = client(m.fetch);
    const dead = await c.webhooks.deliveries.list({ status: 'dead', subscriptionId: 's1', limit: 5 });
    expect(dead[0]).toMatchObject({ subscriptionId: 's1', eventType: 'asset.changed', lastStatusCode: 500, attempts: 8 });
    const again = await c.webhooks.deliveries.redeliver('d1');
    expect(again.status).toBe('pending');
    expect(m.calls.map((x) => [x.method, path(x.url)])).toEqual([
      ['GET', '/api/v1/admin/webhooks/deliveries'],
      ['POST', '/api/v1/admin/webhooks/deliveries/d1/redeliver'],
    ]);
    expect(query(m.calls[0].url)).toEqual({ status: 'dead', subscription_id: 's1', limit: '5' });
    expect(m.calls[1].body).toBeUndefined();
  });

  it('rejects empty ids', () => {
    const c = client(mockFetch().fetch);
    expect(() => c.webhooks.subscriptions.get('')).toThrow(/subscriptionId/);
    expect(() => c.webhooks.deliveries.redeliver(' ')).toThrow(/deliveryId/);
  });
});

describe('client.send', () => {
  it('sends any method with a query and a JSON body', async () => {
    const m = mockFetch(json({ some_field: 1 }));
    const res = await client(m.fetch).send<{ someField: number }>('put', '/x', {
      query: { fooBar: 'a' },
      body: { aB: [{ cD: new Date('2026-01-01T00:00:00Z') }] },
    });
    expect(res).toEqual({ someField: 1 });
    expect(m.calls[0].method).toBe('PUT');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/x?foo_bar=a');
    expect(JSON.parse(m.calls[0].body as string)).toEqual({ a_b: [{ c_d: '2026-01-01T00:00:00.000Z' }] });
  });
});
