import crypto from 'node:crypto';

export const COOKIE = 'modelbudget_admin_session';
export const TTL = 3600;
export const NO_STORE = { 'Cache-Control': 'no-store, private', 'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff' };

export function configuration(env = process.env) {
  const secret = env.DASHBOARD_SESSION_SECRET;
  const password = env.DASHBOARD_LOGIN_PASSWORD;
  if (typeof secret !== 'string' || secret.length < 32 || typeof password !== 'string' || password.length < 24) throw new Error('Dashboard configuration unavailable');
  // Rotating either credential invalidates every session, including old tokens.
  const signingKey = crypto.createHmac('sha256', secret).update('modelbudget-session-v2\0').update(password).digest();
  return { password, signingKey, secure: env.NODE_ENV === 'production' };
}

export function passwordMatches(value, expected) {
  if (typeof value !== 'string' || value.length > 256) return false;
  const digest = input => crypto.createHash('sha256').update(input).digest();
  return crypto.timingSafeEqual(digest(value), digest(expected));
}

export function issueToken(config, now = Date.now()) {
  const payload = Buffer.from(JSON.stringify({ v: 2, iat: now, exp: now + TTL * 1000, nonce: crypto.randomBytes(24).toString('base64url') })).toString('base64url');
  return payload + '.' + crypto.createHmac('sha256', config.signingKey).update(payload).digest('base64url');
}

export function verifyToken(token, config, now = Date.now()) {
  try {
    if (typeof token !== 'string' || token.length > 1024 || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$/.test(token)) return false;
    const [payload, signature] = token.split('.');
    const expected = crypto.createHmac('sha256', config.signingKey).update(payload).digest('base64url');
    if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) return false;
    const decoded = Buffer.from(payload, 'base64url');
    if (decoded.toString('base64url') !== payload) return false;
    const data = JSON.parse(decoded.toString('utf8'));
    return data.v === 2 && Number.isSafeInteger(data.iat) && Number.isSafeInteger(data.exp)
      && data.iat <= now && data.exp > now && data.exp - data.iat === TTL * 1000
      && typeof data.nonce === 'string' && /^[A-Za-z0-9_-]{32}$/.test(data.nonce);
  } catch { return false; }
}

export function cookieOptions(config) {
  return { httpOnly: true, sameSite: 'strict', secure: config.secure, path: '/', maxAge: TTL };
}

export function sameOrigin(request, env = process.env) {
  try {
    const origin = request.headers.get('origin');
    if (!origin || origin === 'null') return false;
    const configured = env.DASHBOARD_ORIGIN;
    if (configured) {
      const parsed = new URL(configured);
      if (parsed.origin !== configured || parsed.username || parsed.password) return false;
      if (env.NODE_ENV === 'production' && parsed.protocol !== 'https:') return false;
      return origin === configured;
    }
    if (env.NODE_ENV === 'production') return false;
    // Development fallback is loopback only, not a user-controlled Host header.
    return origin === 'http://127.0.0.1:3000' || origin === 'http://localhost:3000';
  } catch { return false; }
}

export async function boundedJson(request, maxBytes = 2048) {
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new Error('Invalid request');
  if (Number(request.headers.get('content-length')) > maxBytes) throw new Error('Invalid request');
  if (!request.body) throw new Error('Invalid request');
  const reader = request.body.getReader();
  let length = 0;
  const chunks = [];
  try {
    while (true) {
      // A body that never completes must not keep a login worker busy forever.
      let timeout;
      const stalled = new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('Invalid request')), 5000); });
      let result;
      try { result = await Promise.race([reader.read(), stalled]); }
      finally { clearTimeout(timeout); }
      const { done, value } = result;
      if (done) break;
      length += value.byteLength;
      if (length > maxBytes) { await reader.cancel(); throw new Error('Invalid request'); }
      chunks.push(value);
    }
    const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Invalid request');
    return parsed;
  } finally { reader.releaseLock(); }
}

// Constant-memory local guard, NOT a distributed limiter. Each process has its
// own allowance. Public deployment also requires a shared/edge login limiter.
export function createLoginGuard(limit = 10, windowMs = 60000) {
  let attempts = [];
  return (now = performance.now()) => {
    attempts = attempts.filter(time => time > now - windowMs);
    if (attempts.length >= limit) return false;
    attempts.push(now);
    return true;
  };
}

const resources = new Set(['overview', 'teams', 'usage', 'prompt-versions', 'provider-status']);
export function backendRequest(resource, search, env = process.env) {
  if (!resources.has(resource)) throw new Error('Unknown resource');
  const url = new URL(env.BACKEND_ADMIN_BASE_URL || '');
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Invalid backend');
  if (url.protocol === 'http:' && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('Backend requires HTTPS');
  const key = env.MODEL_BUDGET_ADMIN_API_KEY;
  if (!key || key.length < 32 || /[^\x21-\x7e]/.test(key)) throw new Error('Invalid backend');
  url.pathname = '/admin/v1/' + resource;
  for (const name of search.keys()) if (name !== 'limit') throw new Error('Invalid query');
  const limits = search.getAll('limit');
  if (limits.length > 1 || (limits.length && !/^(?:[1-9]\d?|100)$/.test(limits[0]))) throw new Error('Invalid query');
  if (limits.length) url.searchParams.set('limit', limits[0]);
  return { url, key };
}

export async function fetchBackend(target, fetcher = fetch) {
  try {
    const response = await fetcher(target.url, { headers: { 'X-ModelBudget-Admin-Key': target.key }, cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(5000) });
    if (!response.ok) return { status: 503, data: { error: 'Backend unavailable' } };
    const data = await boundedJson(response, 1024 * 1024);
    return { status: 200, data };
  } catch { return { status: 503, data: { error: 'Backend unavailable' } }; }
}
