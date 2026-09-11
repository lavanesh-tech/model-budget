import { NextRequest, NextResponse } from 'next/server';
import { hasSession } from '@/lib/session';
import { backendRequest, fetchBackend, NO_STORE } from '@/lib/security.mjs';

export const runtime = 'nodejs';
export async function GET(request: NextRequest, context: { params: Promise<{ resource: string }> }) {
  if (!(await hasSession())) return NextResponse.json({ error: 'Not authenticated' }, { status: 401, headers: NO_STORE });
  const { resource } = await context.params;
  let target;
  try { target = backendRequest(resource, request.nextUrl.searchParams); }
  catch { return NextResponse.json({ error: 'Request unavailable' }, { status: 400, headers: NO_STORE }); }
  const result = await fetchBackend(target);
  return NextResponse.json(result.data, { status: result.status, headers: NO_STORE });
}
