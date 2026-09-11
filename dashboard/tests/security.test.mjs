import assert from 'node:assert/strict';
import test from 'node:test';
import {
  NO_STORE, TTL, backendRequest, boundedJson, configuration, cookieOptions,
  createLoginGuard, fetchBackend, issueToken, passwordMatches, sameOrigin,
  verifyToken,
} from '../lib/security.mjs';

const env = {
  NODE_ENV: 'test',
  DASHBOARD_LOGIN_PASSWORD: 'p'.repeat(32),
  DASHBOARD_SESSION_SECRET: 's'.repeat(48),
  BACKEND_ADMIN_BASE_URL: 'http://127.0.0.1:8000',
  MODEL_BUDGET_ADMIN_API_KEY: 'k'.repeat(40),
};

function request(body, headers = { 'content-type': 'application/json' }) {
  return new Request('http://127.0.0.1:3000/api/session', { method: 'POST', headers, body });
}

test('password comparison and signed tokens reject altered or expired sessions', () => {
  const config = configuration(env);
  assert.equal(passwordMatches(env.DASHBOARD_LOGIN_PASSWORD, config.password), true);
  assert.equal(passwordMatches('wrong', config.password), false);
  assert.equal(passwordMatches('x'.repeat(257), config.password), false);
  const now = 1_700_000_000_000;
  const token = issueToken(config, now);
  assert.equal(verifyToken(token, config, now), true);
  assert.equal(verifyToken(token, config, now + TTL * 1000), false);
  assert.equal(verifyToken(token + '.extra', config, now), false);
  assert.equal(verifyToken(token.slice(0, -1) + (token.endsWith('A') ? 'B' : 'A'), config, now), false);
  assert.notEqual(issueToken(config, now), issueToken(config, now));
  assert.equal(verifyToken(token, configuration({ ...env, DASHBOARD_SESSION_SECRET: 't'.repeat(48) }), now), false);
  assert.deepEqual(cookieOptions(config), { httpOnly: true, sameSite: 'strict', secure: false, path: '/', maxAge: TTL });
});

test('requests need a known same origin; production requires configured HTTPS origin', () => {
  const make = origin => new Request('http://127.0.0.1:3000/api/session', { headers: origin ? { origin } : {} });
  assert.equal(sameOrigin(make('http://127.0.0.1:3000'), env), true);
  assert.equal(sameOrigin(make('http://evil.example'), env), false);
  assert.equal(sameOrigin(make(null), env), false);
  const production = { ...env, NODE_ENV: 'production', DASHBOARD_ORIGIN: 'https://dashboard.example.com' };
  assert.equal(sameOrigin(make('https://dashboard.example.com'), production), true);
  assert.equal(sameOrigin(make('http://dashboard.example.com'), production), false);
  assert.equal(sameOrigin(make('https://dashboard.example.com'), { ...env, NODE_ENV: 'production' }), false);
});

test('login JSON is bounded and only accepts a JSON object', async () => {
  assert.deepEqual(await boundedJson(request(JSON.stringify({ password: 'ok' }))), { password: 'ok' });
  for (const value of ['null', '[]', '"text"']) await assert.rejects(boundedJson(request(value)));
  await assert.rejects(boundedJson(request('{', { 'content-type': 'text/plain' })));
  await assert.rejects(boundedJson(request(JSON.stringify({ password: 'x'.repeat(3000) }))));
});

test('local login guard is bounded and recovers after its window', () => {
  const guard = createLoginGuard(2, 100);
  assert.equal(guard(0), true);
  assert.equal(guard(1), true);
  assert.equal(guard(2), false);
  assert.equal(guard(101), true);
});

test('backend proxy target is allowlisted and backend failures are sanitized', async () => {
  const target = backendRequest('usage', new URLSearchParams('limit=50'), env);
  assert.equal(target.url.href, 'http://127.0.0.1:8000/admin/v1/usage?limit=50');
  assert.throws(() => backendRequest('usage', new URLSearchParams('limit=999'), env));
  assert.throws(() => backendRequest('unknown', new URLSearchParams(), env));
  assert.throws(() => backendRequest('usage', new URLSearchParams(), { ...env, BACKEND_ADMIN_BASE_URL: 'https://token@api.example/' }));
  const failed = await fetchBackend(target, async () => new Response(JSON.stringify({ api_key: 'must-not-leak' }), { status: 500, headers: { 'content-type': 'application/json' } }));
  assert.deepEqual(failed, { status: 503, data: { error: 'Backend unavailable' } });
  const ok = await fetchBackend(target, async (_url, options) => {
    assert.equal(options.cache, 'no-store');
    assert.equal(options.redirect, 'error');
    assert.equal(options.headers['X-ModelBudget-Admin-Key'], env.MODEL_BUDGET_ADMIN_API_KEY);
    return new Response(JSON.stringify({ items: [] }), { headers: { 'content-type': 'application/json' } });
  });
  assert.deepEqual(ok, { status: 200, data: { items: [] } });
  assert.equal(NO_STORE['Cache-Control'], 'no-store, private');
});
