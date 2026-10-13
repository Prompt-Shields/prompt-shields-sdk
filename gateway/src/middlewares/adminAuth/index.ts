import { Context, Next } from 'hono';
import { env } from 'hono/adapter';

const SESSION_COOKIE_NAME = 'portkey_admin_session';
const SESSION_MAX_AGE_SECONDS = 60 * 60 * 12; // 12 hours
const adminSessions = new Map<string, number>();

// PS fork: upstream reads `admin_token` from a committed conf.json. Our fork
// gitignores conf.json, so the token comes from the environment instead.
export const ADMIN_TOKEN_ENV = 'PS_GATEWAY_ADMIN_TOKEN';
const MIN_ADMIN_TOKEN_LENGTH = 16;

export const readAdminToken = (c?: Context): string | undefined => {
  const value = c
    ? env<Record<string, string | undefined>>(c)[ADMIN_TOKEN_ENV]
    : typeof process !== 'undefined'
      ? process.env[ADMIN_TOKEN_ENV]
      : undefined;
  const token = typeof value === 'string' ? value.trim() : '';
  return token.length >= MIN_ADMIN_TOKEN_LENGTH ? token : undefined;
};

const getConfiguredAdminToken = (c: Context): string => {
  const adminToken = readAdminToken(c);
  if (!adminToken) {
    throw new Error(
      `Admin UI auth requires ${ADMIN_TOKEN_ENV} (at least ${MIN_ADMIN_TOKEN_LENGTH} characters). Set it or start the gateway with --headless.`
    );
  }
  return adminToken;
};

// Constant-time comparison so the token cannot be recovered via timing.
const tokensMatch = (candidate: unknown, expected: string): boolean => {
  if (typeof candidate !== 'string') return false;
  const a = new TextEncoder().encode(candidate);
  const b = new TextEncoder().encode(expected);
  let diff = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) diff |= (a[i] ?? 0) ^ b[i];
  return diff === 0;
};

const parseCookies = (cookieHeader?: string): Record<string, string> => {
  if (!cookieHeader) return {};

  return cookieHeader.split(';').reduce(
    (acc, cookiePart) => {
      const [rawKey, ...valueParts] = cookiePart.trim().split('=');
      if (!rawKey) return acc;
      try {
        acc[rawKey] = decodeURIComponent(valueParts.join('='));
      } catch {
        // PS fork: a malformed cookie is treated as absent (401), not a 500.
      }
      return acc;
    },
    {} as Record<string, string>
  );
};

const getSessionId = (c: Context): string | undefined => {
  const cookies = parseCookies(c.req.header('cookie'));
  return cookies[SESSION_COOKIE_NAME];
};

const getBearerToken = (c: Context): string | undefined => {
  const authHeader = c.req.header('authorization');
  if (!authHeader) return undefined;

  const [scheme, token] = authHeader.trim().split(/\s+/, 2);
  if (!scheme || !token) return undefined;
  if (scheme.toLowerCase() !== 'bearer') return undefined;

  return token;
};

const isSessionActive = (sessionId?: string): boolean => {
  if (!sessionId) return false;

  const expiresAt = adminSessions.get(sessionId);
  if (!expiresAt) return false;

  if (expiresAt < Date.now()) {
    adminSessions.delete(sessionId);
    return false;
  }

  return true;
};

const createSession = (): string => {
  // PS fork: drop expired sessions so the map cannot grow without bound.
  const now = Date.now();
  for (const [id, expiresAt] of adminSessions) {
    if (expiresAt < now) adminSessions.delete(id);
  }
  const sessionId = crypto.randomUUID();
  const expiresAt = Date.now() + SESSION_MAX_AGE_SECONDS * 1000;
  adminSessions.set(sessionId, expiresAt);
  return sessionId;
};

const setSessionCookie = (c: Context, sessionId: string) => {
  c.header(
    'Set-Cookie',
    `${SESSION_COOKIE_NAME}=${encodeURIComponent(sessionId)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${SESSION_MAX_AGE_SECONDS}`
  );
};

export const adminAuthMiddleware = async (c: Context, next: Next) => {
  let configuredToken: string;
  try {
    configuredToken = getConfiguredAdminToken(c);
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : 'Admin UI auth is misconfigured.';
    return c.json({ status: 'failure', message }, 500);
  }

  const hasValidSession = isSessionActive(getSessionId(c));
  const hasValidBearerToken = tokensMatch(getBearerToken(c), configuredToken);

  if (!hasValidSession && !hasValidBearerToken) {
    return c.json(
      {
        status: 'failure',
        message:
          'Admin authentication required. Use a valid admin session cookie or Authorization: Bearer <admin_token>.',
      },
      401
    );
  }

  await next();
};

export const adminAuthSessionStatusHandler = (c: Context) => {
  try {
    getConfiguredAdminToken(c);
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : 'Admin UI auth is misconfigured.';
    return c.json({ status: 'failure', message }, 500);
  }

  return c.json({ authenticated: isSessionActive(getSessionId(c)) });
};

export const adminAuthLoginHandler = async (c: Context) => {
  let configuredToken: string;
  try {
    configuredToken = getConfiguredAdminToken(c);
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : 'Admin UI auth is misconfigured.';
    return c.json({ status: 'failure', message }, 500);
  }

  let body: { admin_token?: string } = {};
  try {
    body = await c.req.json();
  } catch {
    return c.json(
      {
        status: 'failure',
        message: 'Invalid request body. Expected JSON with admin_token.',
      },
      400
    );
  }

  if (!tokensMatch(body?.admin_token, configuredToken)) {
    return c.json(
      {
        status: 'failure',
        message: 'Invalid admin token.',
      },
      401
    );
  }

  const sessionId = createSession();
  setSessionCookie(c, sessionId);
  return c.json({ authenticated: true });
};
