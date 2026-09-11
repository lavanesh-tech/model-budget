import { NextRequest, NextResponse } from "next/server";
import { hasSession } from "@/lib/session";
const allowed = new Set(["overview", "teams", "usage", "prompt-versions", "provider-status"]);
export async function GET(request: NextRequest, context: { params: Promise<{ resource: string }> }) {
  if (!(await hasSession())) return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  const { resource } = await context.params;
  if (!allowed.has(resource)) return NextResponse.json({ error: "Not found" }, { status: 404 });
  const base = process.env.BACKEND_ADMIN_BASE_URL;
  const key = process.env.MODEL_BUDGET_ADMIN_API_KEY;
  if (!base || !key) return NextResponse.json({ error: "Dashboard backend is not configured" }, { status: 503 });
  const url = new URL(`/admin/v1/${resource}`, base);
  const limit = request.nextUrl.searchParams.get("limit"); if (limit) url.searchParams.set("limit", limit);
  try { const response = await fetch(url, { headers: { "X-ModelBudget-Admin-Key": key }, cache: "no-store" }); const data = await response.json(); return NextResponse.json(data, { status: response.status, headers: { "Cache-Control": "no-store" } }); }
  catch { return NextResponse.json({ error: "Backend unavailable" }, { status: 503 }); }
}
