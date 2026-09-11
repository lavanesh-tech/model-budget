import { NextRequest, NextResponse } from 'next/server';
import { clearSession, createSession } from '@/lib/session';
import { boundedJson, configuration, createLoginGuard, NO_STORE, passwordMatches, sameOrigin } from '@/lib/security.mjs';

export const runtime = 'nodejs';
const allowLogin = createLoginGuard();
function reply(status: number, error: string) {
  return NextResponse.json({ error }, { status, headers: NO_STORE });
}

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return reply(403, 'Request rejected');
  if (!allowLogin()) return NextResponse.json({ error: 'Try again later' }, { status: 429, headers: { ...NO_STORE, 'Retry-After': '60' } });
  let config;
  try { config = configuration(); } catch { return reply(503, 'Login unavailable'); }
  let body;
  try { body = await boundedJson(request); } catch { return reply(400, 'Invalid request'); }
  if (!passwordMatches(body.password, config.password)) return reply(401, 'Invalid credentials');
  try { await createSession(); } catch { return reply(503, 'Login unavailable'); }
  return NextResponse.json({ ok: true }, { headers: NO_STORE });
}

export async function DELETE(request: NextRequest) {
  if (!sameOrigin(request)) return reply(403, 'Request rejected');
  try { await clearSession(); } catch { return reply(503, 'Logout unavailable'); }
  return NextResponse.json({ ok: true }, { headers: NO_STORE });
}
