import { NextResponse } from 'next/server';

// New in Step 39, purely for container orchestration: unauthenticated,
// no session check, no backend call, no side effects -- deliberately
// separate from every /api/admin/* and /api/session route, which stay
// exactly as Step 38 left them. Existing solely so Docker's
// HEALTHCHECK (see Dockerfile) has something to probe that proves the
// Node process is actually serving requests.
export const runtime = 'nodejs';

export async function GET() {
  return NextResponse.json({ status: 'ok' }, { headers: { 'Cache-Control': 'no-store' } });
}
