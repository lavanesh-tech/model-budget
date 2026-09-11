'use client';
import { useState } from 'react';

export default function Login() {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try {
      const response = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }), cache: 'no-store' });
      if (!response.ok) {
        setError(response.status === 429 ? 'Too many attempts. Wait one minute.' : response.status === 401 ? 'Invalid credentials.' : 'Login unavailable. Please try again.');
        return;
      }
      setPassword('');
      // Full navigation avoids retaining an unauthenticated server-page cache.
      window.location.replace('/');
    } catch { setError('Unable to connect. Please try again.'); }
    finally { setBusy(false); }
  }
  return <main className="login"><form onSubmit={submit}><h1>ModelBudget</h1><p>Operator dashboard</p>
    <label>Dashboard password<input autoFocus type="password" autoComplete="current-password" maxLength={256} value={password} onChange={event => setPassword(event.target.value)} required /></label>
    {error && <p className="error" role="alert">{error}</p>}<button disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
  </form></main>;
}
