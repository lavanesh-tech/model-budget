export const COOKIE: string;
export const TTL: number;
export const NO_STORE: Record<string, string>;

export interface DashboardConfig {
  password: string;
  signingKey: Buffer;
  secure: boolean;
}

export function configuration(env?: NodeJS.ProcessEnv): DashboardConfig;
export function passwordMatches(value: unknown, expected: string): boolean;
export function issueToken(config: DashboardConfig, now?: number): string;
export function verifyToken(token: unknown, config: DashboardConfig, now?: number): boolean;
export function cookieOptions(config: DashboardConfig): {
  httpOnly: boolean; sameSite: 'strict'; secure: boolean; path: string; maxAge: number;
};
export function sameOrigin(request: Request, env?: NodeJS.ProcessEnv): boolean;
export function boundedJson(request: Request | Response, maxBytes?: number): Promise<Record<string, unknown>>;
export function createLoginGuard(limit?: number, windowMs?: number): (now?: number) => boolean;
export function backendRequest(resource: string, search: URLSearchParams, env?: NodeJS.ProcessEnv): { url: URL; key: string };
export function fetchBackend(target: { url: URL; key: string }, fetcher?: typeof fetch): Promise<{ status: number; data: unknown }>;
