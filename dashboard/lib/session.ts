import crypto from "node:crypto";
import { cookies } from "next/headers";

const COOKIE = "modelbudget_admin_session";
const maxAge = 60 * 60 * 8;
function secret() { const value = process.env.DASHBOARD_SESSION_SECRET; if (!value || value.length < 32) throw new Error("Dashboard session is not configured"); return value; }
function sign(payload: string) { return crypto.createHmac("sha256", secret()).update(payload).digest("base64url"); }
export function validPassword(value: unknown) { const expected = process.env.DASHBOARD_LOGIN_PASSWORD; if (typeof value !== "string" || !expected) return false; const received = Buffer.from(value); const target = Buffer.from(expected); return received.length === target.length && crypto.timingSafeEqual(received, target); }
export async function createSession() { const payload = Buffer.from(JSON.stringify({ exp: Date.now() + maxAge * 1000 })).toString("base64url"); (await cookies()).set(COOKIE, `${payload}.${sign(payload)}`, { httpOnly: true, sameSite: "strict", secure: process.env.NODE_ENV === "production", path: "/", maxAge }); }
export async function hasSession() { const raw = (await cookies()).get(COOKIE)?.value; if (!raw) return false; const [payload, signature] = raw.split("."); if (!payload || !signature) return false; const received = Buffer.from(signature); const target = Buffer.from(sign(payload)); if (received.length !== target.length || !crypto.timingSafeEqual(received, target)) return false; try { return JSON.parse(Buffer.from(payload, "base64url").toString()).exp > Date.now(); } catch { return false; } }
export async function clearSession() { (await cookies()).delete(COOKIE); }
