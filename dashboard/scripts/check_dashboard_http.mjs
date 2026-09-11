import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const root = process.cwd();
const loginPassword = 'step38-test-password-not-a-real-secret';
const sessionSecret = 's'.repeat(48);
const adminKey = 'k'.repeat(40);

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}

async function stop(child) {
  if (child.exitCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([once(child, 'exit'), delay(5000)]);
  if (child.exitCode === null) child.kill('SIGKILL');
}

const backend = createServer((request, response) => {
  if (request.headers['x-modelbudget-admin-key'] !== adminKey) {
    response.writeHead(404).end();
    return;
  }
  if (request.url?.includes('provider-status')) {
    response.writeHead(500, { 'content-type': 'application/json' }).end(JSON.stringify({ secret: 'must-not-leak' }));
    return;
  }
  response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ items: [], active_teams: 0, current_budget_remaining_usd: '0', usage_succeeded: 0, usage_failed: 0 }));
});

const backendPort = await listen(backend);
const probe = createServer();
const dashboardPort = await listen(probe);
await new Promise(resolve => probe.close(resolve));
const origin = `http://127.0.0.1:${dashboardPort}`;
const next = spawn(process.execPath, [path.join(root, 'node_modules', 'next', 'dist', 'bin', 'next'), 'dev', '--hostname', '127.0.0.1', '--port', String(dashboardPort)], {
  cwd: root,
  stdio: 'ignore',
  env: {
    ...process.env,
    NEXT_TELEMETRY_DISABLED: '1',
    DASHBOARD_ORIGIN: origin,
    DASHBOARD_LOGIN_PASSWORD: loginPassword,
    DASHBOARD_SESSION_SECRET: sessionSecret,
    BACKEND_ADMIN_BASE_URL: `http://127.0.0.1:${backendPort}`,
    MODEL_BUDGET_ADMIN_API_KEY: adminKey,
  },
});

try {
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      const response = await fetch(`${origin}/login`);
      if (response.ok) { ready = true; break; }
    } catch {}
    await delay(250);
  }
  assert.equal(ready, true, 'temporary dashboard did not start');

  let response = await fetch(`${origin}/api/admin/overview`);
  assert.equal(response.status, 401);
  assert.match(response.headers.get('cache-control') || '', /no-store/);

  response = await fetch(`${origin}/api/session`, {
    method: 'POST', headers: { origin: 'http://evil.example', 'content-type': 'application/json' },
    body: JSON.stringify({ password: loginPassword }),
  });
  assert.equal(response.status, 403);

  response = await fetch(`${origin}/api/session`, {
    method: 'POST', headers: { origin, 'content-type': 'application/json' },
    body: JSON.stringify({ password: loginPassword }),
  });
  assert.equal(response.status, 200);
  const cookie = response.headers.get('set-cookie')?.split(';', 1)[0];
  assert.ok(cookie?.startsWith('modelbudget_admin_session='));
  assert.match(response.headers.get('set-cookie') || '', /HttpOnly/i);
  assert.match(response.headers.get('set-cookie') || '', /SameSite=Strict/i);

  response = await fetch(`${origin}/api/admin/usage?limit=50`, { headers: { cookie } });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { items: [], active_teams: 0, current_budget_remaining_usd: '0', usage_succeeded: 0, usage_failed: 0 });

  response = await fetch(`${origin}/api/admin/provider-status`, { headers: { cookie } });
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: 'Backend unavailable' });

  response = await fetch(`${origin}/api/session`, { method: 'DELETE', headers: { origin, cookie } });
  assert.equal(response.status, 200);
  assert.match(response.headers.get('set-cookie') || '', /Max-Age=0/i);

  response = await fetch(`${origin}/api/session`, {
    method: 'POST', headers: { origin, 'content-type': 'text/plain' }, body: 'not-json',
  });
  assert.equal(response.status, 400);

  console.log('Dashboard HTTP checks passed: login, cookie, authenticated proxy, sanitized backend failure, and logout.');
} finally {
  await stop(next);
  await new Promise(resolve => backend.close(resolve));
}
