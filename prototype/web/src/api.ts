const TOKEN_KEY = 'edith.session';
let token = sessionStorage.getItem(TOKEN_KEY) || '';
export function setToken(value: string) { token = value; sessionStorage.setItem(TOKEN_KEY, value); }
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (token) headers.set('X-Edith-Session', token);
  if (options.body && typeof options.body === 'string') headers.set('Content-Type', 'application/json');
  let response: Response;
  try { response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' }); }
  catch (error) { if (error instanceof DOMException && error.name === 'AbortError') throw error; throw new Error('The local EDITH hub is not responding. Check that it is running and try again.'); }
  const body = await response.json().catch(() => null) as T & { error?: string } | null;
  if (!response.ok) throw new Error(body?.error || `The request could not be completed (${response.status}).`);
  if (!body) throw new Error('The local hub returned an unreadable response.');
  return body;
}
export const post = <T>(path: string, body: unknown, signal?: AbortSignal) => api<T>(path, { method: 'POST', body: JSON.stringify(body), signal });
export const errorText = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Please try again.';
export const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
