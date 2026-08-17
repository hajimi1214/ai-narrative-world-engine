export const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export class ApiError extends Error { constructor(message: string, public detail?: unknown) { super(message); } }
export async function api(path: string, options?: RequestInit) {
  const response = await fetch(`${apiUrl}${path}`, { ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  if (!response.ok) { const detail = await response.json().catch(() => null); throw new ApiError(detail?.detail?.message || detail?.detail?.code || detail?.detail || `请求失败（${response.status}）`, detail); }
  return response.status === 204 ? null : response.json();
}
