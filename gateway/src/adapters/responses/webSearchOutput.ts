/**
 * PS fork (#73): web search results as Responses output, for targets reached
 * through the Responses adapter (requestFields.ts maps a `web_search` tool to
 * Anthropic's web_search server tool and to Google Search grounding).
 *
 * - Anthropic `server_tool_use` (name `web_search`) + `web_search_tool_result`
 *   blocks → `web_search_call` output items, in block order.
 * - Anthropic text block `citations` of type `web_search_result_location` →
 *   `url_citation` annotations covering that text block.
 * - Google `groundingMetadata` → one `web_search_call` per
 *   `webSearchQueries` entry (after the message and any function calls,
 *   in JSON as in the stream) and `url_citation`
 *   annotations from `groundingSupports` / `groundingChunks[].web`.
 *
 * Only a result's URL (and a citation's URL and title) are copied. Anthropic's
 * `encrypted_content` / `encrypted_index` and the cited text never reach the
 * response. Sources are listed only when the request's `include` asks for
 * `web_search_call.action.sources`, as with the Responses API.
 *
 * Annotation `start_index` / `end_index` count Unicode code points of the
 * output_text. Google segment offsets are UTF-8 byte offsets and are
 * converted; a segment whose offsets do not match its text is located by its
 * text, or skipped.
 */

import { randomUUID } from 'crypto';

export const WEB_SEARCH_SOURCES_INCLUDE = 'web_search_call.action.sources';

export interface UrlCitation {
  type: 'url_citation';
  start_index: number;
  end_index: number;
  url: string;
  title: string;
}

export type WebSearchCallStatus =
  | 'in_progress'
  | 'searching'
  | 'completed'
  | 'failed';

export interface WebSearchCallItem {
  id: string;
  type: 'web_search_call';
  status: WebSearchCallStatus;
  action: {
    type: 'search';
    query: string;
    sources?: { type: 'url'; url: string }[];
  };
}

/** Whether the request's `include` lists web search sources. */
export function wantsWebSearchSources(originalRequest?: any): boolean {
  const include = originalRequest?.include;
  return Array.isArray(include) && include.includes(WEB_SEARCH_SOURCES_INCLUDE);
}

export function newWebSearchCallId(): string {
  return `ws_${randomUUID().replace(/-/g, '')}`;
}

export function webSearchCallItem(
  id: string,
  status: WebSearchCallStatus,
  query: string,
  sources?: string[]
): WebSearchCallItem {
  return {
    id,
    type: 'web_search_call',
    status,
    action: {
      type: 'search',
      query,
      ...(sources
        ? { sources: sources.map((url) => ({ type: 'url', url })) }
        : {}),
    },
  };
}

/** Number of Unicode code points in `s` (low surrogates are not counted). */
export function codePointLength(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0xdc00 || c > 0xdfff) n++;
  }
  return n;
}

// ── Anthropic ───────────────────────────────────────────────────────────────

/** The query of a web_search server_tool_use input (object or JSON text). */
export function anthropicSearchQuery(input: unknown): string {
  let value: any = input;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return '';
    }
  }
  return typeof value?.query === 'string' ? value.query : '';
}

/**
 * Status and result URLs of a `web_search_tool_result` block's content: a
 * list of `web_search_result` entries, or an error object.
 */
export function anthropicSearchResult(content: unknown): {
  status: 'completed' | 'failed';
  urls: string[];
} {
  if (!Array.isArray(content)) return { status: 'failed', urls: [] };
  const urls: string[] = [];
  for (const r of content) {
    if (
      r?.type === 'web_search_result' &&
      typeof r.url === 'string' &&
      !urls.includes(r.url)
    ) {
      urls.push(r.url);
    }
  }
  return { status: 'completed', urls };
}

/**
 * A web_search_result_location citation as a url_citation (URL and title
 * only), or null for any other citation type.
 */
export function anthropicCitation(
  citation: any,
  start: number,
  end: number
): UrlCitation | null {
  if (
    citation?.type !== 'web_search_result_location' ||
    typeof citation.url !== 'string'
  ) {
    return null;
  }
  return {
    type: 'url_citation',
    start_index: start,
    end_index: end,
    url: citation.url,
    title: typeof citation.title === 'string' ? citation.title : '',
  };
}

/** url_citations for one text block's citations, one per distinct URL. */
export function anthropicBlockAnnotations(
  citations: unknown,
  start: number,
  end: number
): UrlCitation[] {
  const out: UrlCitation[] = [];
  if (!Array.isArray(citations) || end <= start) return out;
  for (const c of citations) {
    const a = anthropicCitation(c, start, end);
    if (a && !out.some((o) => o.url === a.url)) out.push(a);
  }
  return out;
}

export type AnthropicSegment =
  | { kind: 'text'; text: string; annotations: UrlCitation[] }
  | { kind: 'web_search_call'; item: WebSearchCallItem };

/** Whether Anthropic content blocks carry web search calls or citations. */
export function hasAnthropicWebSearch(blocks: unknown): boolean {
  return (
    Array.isArray(blocks) &&
    blocks.some(
      (b) =>
        (b?.type === 'server_tool_use' && b.name === 'web_search') ||
        b?.type === 'web_search_tool_result' ||
        (b?.type === 'text' &&
          Array.isArray(b.citations) &&
          b.citations.some(
            (c: any) => c?.type === 'web_search_result_location'
          ))
    )
  );
}

/**
 * Anthropic content blocks (a non-streamed message's `content_blocks`) as
 * text segments with annotations and web_search_call items, in block order.
 * Consecutive text blocks form one segment; a web search call starts a new
 * one, as the streamed output does.
 */
export function anthropicWebSearchSegments(
  blocks: any[],
  includeSources: boolean
): AnthropicSegment[] {
  const segments: AnthropicSegment[] = [];
  // a Map: tool ids come from the provider (a `__proto__` key must not
  // reach Object.prototype)
  const calls = new Map<unknown, WebSearchCallItem>();
  let text: Extract<AnthropicSegment, { kind: 'text' }> | null = null;
  let textLength = 0;
  for (const block of blocks) {
    if (block?.type === 'text' && typeof block.text === 'string') {
      if (!text) {
        text = { kind: 'text', text: '', annotations: [] };
        textLength = 0;
        segments.push(text);
      }
      const start = textLength;
      text.text += block.text;
      textLength += codePointLength(block.text);
      text.annotations.push(
        ...anthropicBlockAnnotations(block.citations, start, textLength)
      );
    } else if (
      block?.type === 'server_tool_use' &&
      block.name === 'web_search'
    ) {
      const item = webSearchCallItem(
        newWebSearchCallId(),
        // no result block (e.g. a paused turn): the search did not complete
        'failed',
        anthropicSearchQuery(block.input),
        includeSources ? [] : undefined
      );
      if (typeof block.id === 'string') calls.set(block.id, item);
      segments.push({ kind: 'web_search_call', item });
      text = null;
    } else if (block?.type === 'web_search_tool_result') {
      const item = calls.get(block.tool_use_id);
      if (!item) continue;
      const { status, urls } = anthropicSearchResult(block.content);
      item.status = status;
      if (includeSources) {
        item.action.sources = urls.map((url) => ({ type: 'url', url }));
      }
    }
  }
  return segments;
}

// ── Google ──────────────────────────────────────────────────────────────────

function utf8Length(codePoint: number): number {
  if (codePoint < 0x80) return 1;
  if (codePoint < 0x800) return 2;
  if (codePoint < 0x10000) return 3;
  return 4;
}

/** The code point index at UTF-8 byte offset `bytes` of `text`, or -1. */
function byteOffsetToCodePoint(text: string, bytes: number): number {
  let b = 0;
  let i = 0;
  for (const ch of text) {
    if (b === bytes) return i;
    if (b > bytes) return -1;
    b += utf8Length(ch.codePointAt(0)!);
    i++;
  }
  return b === bytes ? i : -1;
}

function codePointSlice(text: string, start: number, end: number): string {
  return Array.from(text).slice(start, end).join('');
}

/** A grounding segment's [start, end) code point range in `text`, or null. */
function segmentRange(text: string, segment: any): [number, number] | null {
  if (!segment || typeof segment !== 'object') return null;
  const segText = typeof segment.text === 'string' ? segment.text : undefined;
  // proto3 JSON leaves out a zero startIndex
  const startByte = Number.isInteger(segment.startIndex)
    ? segment.startIndex
    : 0;
  if (Number.isInteger(segment.endIndex)) {
    const start = byteOffsetToCodePoint(text, startByte);
    const end = byteOffsetToCodePoint(text, segment.endIndex);
    if (
      start >= 0 &&
      end > start &&
      (segText === undefined || codePointSlice(text, start, end) === segText)
    ) {
      return [start, end];
    }
  }
  // Offsets relative to another part, or not matching: find the text
  if (segText) {
    const at = text.indexOf(segText);
    if (at >= 0) {
      const start = codePointLength(text.slice(0, at));
      return [start, start + codePointLength(segText)];
    }
  }
  return null;
}

/**
 * Grounding metadata from several stream chunks as one: queries and web
 * chunks deduplicated, support chunk indices renumbered to match.
 */
export function mergeGroundingMetadata(list: any[]): any {
  if (list.length === 1) return list[0];
  const queries: string[] = [];
  const chunks: any[] = [];
  const supports: any[] = [];
  for (const g of list) {
    for (const q of Array.isArray(g?.webSearchQueries)
      ? g.webSearchQueries
      : []) {
      if (typeof q === 'string' && !queries.includes(q)) queries.push(q);
    }
    const local: any[] = Array.isArray(g?.groundingChunks)
      ? g.groundingChunks
      : [];
    const remap = local.map((c) => {
      const uri = c?.web?.uri;
      const at =
        typeof uri === 'string'
          ? chunks.findIndex((x) => x?.web?.uri === uri)
          : -1;
      if (at >= 0) return at;
      chunks.push(c);
      return chunks.length - 1;
    });
    for (const s of Array.isArray(g?.groundingSupports)
      ? g.groundingSupports
      : []) {
      supports.push({
        ...s,
        groundingChunkIndices: (Array.isArray(s?.groundingChunkIndices)
          ? s.groundingChunkIndices
          : []
        ).map((i: unknown) =>
          // an index, not a key such as "length"
          Number.isInteger(i) ? remap[i as number] : undefined
        ),
      });
    }
  }
  return {
    webSearchQueries: queries,
    groundingChunks: chunks,
    groundingSupports: supports,
  };
}

/** Web search calls and annotations from Google grounding metadata. */
export function googleGroundingOutput(
  grounding: any,
  text: string,
  includeSources: boolean
): { calls: WebSearchCallItem[]; annotations: UrlCitation[] } {
  if (!grounding || typeof grounding !== 'object') {
    return { calls: [], annotations: [] };
  }
  const chunks: any[] = Array.isArray(grounding.groundingChunks)
    ? grounding.groundingChunks
    : [];
  const web = (i: unknown) => {
    const w = typeof i === 'number' ? chunks[i]?.web : undefined;
    return typeof w?.uri === 'string' ? w : undefined;
  };

  const annotations: UrlCitation[] = [];
  if (text) {
    for (const support of Array.isArray(grounding.groundingSupports)
      ? grounding.groundingSupports
      : []) {
      const range = segmentRange(text, support?.segment);
      if (!range) continue;
      const indices = Array.isArray(support.groundingChunkIndices)
        ? support.groundingChunkIndices
        : [];
      const seen = new Set<string>();
      for (const i of indices) {
        const w = web(i);
        if (!w || seen.has(w.uri)) continue;
        seen.add(w.uri);
        annotations.push({
          type: 'url_citation',
          start_index: range[0],
          end_index: range[1],
          url: w.uri,
          title: typeof w.title === 'string' ? w.title : '',
        });
      }
    }
    annotations.sort((a, b) => a.start_index - b.start_index);
  }

  const urls: string[] = [];
  for (let i = 0; i < chunks.length; i++) {
    const w = web(i);
    if (w && !urls.includes(w.uri)) urls.push(w.uri);
  }
  const queries: string[] = (
    Array.isArray(grounding.webSearchQueries) ? grounding.webSearchQueries : []
  ).filter((q: unknown): q is string => typeof q === 'string');
  if (!queries.length && urls.length) queries.push('');
  // Sources are not reported per query: they go on the first call
  const calls = queries.map((q, i) =>
    webSearchCallItem(
      newWebSearchCallId(),
      'completed',
      q,
      includeSources ? (i === 0 ? urls : []) : undefined
    )
  );
  return { calls, annotations };
}
