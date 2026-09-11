import { cookies } from 'next/headers';
import { COOKIE, configuration, issueToken, verifyToken, TTL } from './security.mjs';

export async function createSession() {
  const config = configuration();
  (await cookies()).set(COOKIE, issueToken(config), {
    httpOnly: true, sameSite: 'strict', secure: config.secure, path: '/', maxAge: TTL,
  });
}

export async function hasSession() {
  try { return verifyToken((await cookies()).get(COOKIE)?.value, configuration()); }
  catch { return false; }
}

export async function clearSession() {
  (await cookies()).set(COOKIE, '', {
    httpOnly: true, sameSite: 'strict', secure: process.env.NODE_ENV === 'production',
    path: '/', maxAge: 0, expires: new Date(0),
  });
}
