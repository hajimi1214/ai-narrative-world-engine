// Keep the local default on the same IPv4 loopback as the API launcher.
export const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
export class ApiError extends Error { constructor(message: string, public detail?: unknown, public status?: number) { super(message); } }
const errorMessages: Record<string, string> = {
  AUTHOR_EDIT_CONFLICT: "当前工作稿已经更新。请保留修改内容，刷新并比较最新版本后再保存。",
  AUTHOR_EDIT_REQUIRES_PAUSE: "自动创作正在运行，请先到导演台暂停，再保存人工修改。",
  AUTHOR_CONTENT_EMPTY: "正文不能为空。",
  VOLUME_SEALED: "本卷已封存。请先到卷导演台明确解封，再修改正文。",
  CRITIC_OUTPUT_INVALID: "审核模型未返回有效报告，本次未通过审核。请重新运行质量检查。",
  QUALITY_CRITIC_REQUIRED: "当前正文需要有效的模型审核，请重新运行质量检查。",
  WRITER_SOURCE_CHANGED: "章节来源已变化，请刷新章节后重新生成或改稿。",
  MAX_RETRIES_REACHED: "本次检查点的重试次数已用完，请查看失败原因或接管处理。",
  AUTO_CROSS_VOLUME_NEXT_PLAN_REQUIRED: "下一卷目标、核心冲突或结束状态仍未补齐。",
  VOLUME_NEXT_SOURCE_SNAPSHOT_STALE: "承接快照与封存卷不一致，请重新检查下一卷来源。",
};
export async function api(path: string, options?: RequestInit) {
  const response = await fetch(`${apiUrl}${path}`, { ...options, headers: { "Content-Type": "application/json", ...options?.headers } });
  if (!response.ok) { const detail = await response.json().catch(() => null); const body = detail?.detail; const code = body?.code; const validation = Array.isArray(body) ? body.map((item: any) => `${item.loc?.filter((part: string) => part !== "body").join(".") || "输入"}：${item.msg || "格式无效"}`).join("；") : ""; const message = errorMessages[code] || body?.message || code || validation || (typeof body === "string" ? body : `请求失败（${response.status}）`); throw new ApiError(message, detail, response.status); }
  return response.status === 204 ? null : response.json();
}
