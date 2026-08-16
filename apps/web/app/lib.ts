export const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export async function api(path: string, options?: RequestInit) { const response = await fetch(`${apiUrl}${path}`, { ...options, headers: { "Content-Type": "application/json", ...options?.headers } }); if (!response.ok) throw new Error(await response.text()); return response.status === 204 ? null : response.json(); }
