import { NextRequest, NextResponse } from "next/server";
import { clearSession, createSession, validPassword } from "@/lib/session";
export async function POST(request: NextRequest) { const body = await request.json().catch(() => ({})); if (!validPassword(body.password)) return NextResponse.json({ error: "Invalid credentials" }, { status: 401 }); await createSession(); return NextResponse.json({ ok: true }); }
export async function DELETE() { await clearSession(); return NextResponse.json({ ok: true }); }
