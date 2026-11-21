export type RecoveryState = "WAITING_RETRY" | "NEEDS_AUTHOR" | "PAUSED" | "RUNNING" | "COMPLETED" | "QUEUED";

export type RunRecovery = {
  state: RecoveryState;
  next_retry_at: string | null;
  attempts: number;
  max_attempts: number;
  last_checkpoint: { stage: string; saved_at: string } | null;
  error_code: string | null;
  usage_uncertain: boolean;
  reserved_tokens?: number;
};

export type RunSummary = {
  id: string;
  status?: string;
  run_mode?: string;
  current_stage?: string;
  pause_reason?: string | null;
  next_action?: string | null;
  recovery?: RunRecovery | null;
};

export const directorHref = (projectId: string, run: Pick<RunSummary, "id" | "run_mode">) => {
  const director = run.run_mode === "AUTHOR_GUIDED_VOLUME" ? "volume" : "auto";
  return `/projects/${encodeURIComponent(projectId)}/director/${director}?${new URLSearchParams({ run_id: run.id })}`;
};

export function recoveryState(run: RunSummary): RecoveryState | null {
  if (run.status === "PAUSED" && run.current_stage === "DIRECTION_SELECTION") return "NEEDS_AUTHOR";
  if (run.recovery) return run.recovery.state;
  if (run.status === "CREATED") return "QUEUED";
  if (run.status === "FAILED" || run.status === "BLOCKED") return "NEEDS_AUTHOR";
  if (run.status === "PAUSED" || run.status === "RUNNING" || run.status === "COMPLETED") return run.status;
  return null;
}

export const isBudgetPause = (run: RunSummary) => run.status === "PAUSED" && ["RUN_CHAPTER_BUDGET_REACHED", "TOKEN_BUDGET_EXCEEDED", "MODEL_CALL_BUDGET_EXCEEDED"].includes(run.pause_reason || "");

const recoveryLabels: Record<RecoveryState, string> = {
  WAITING_RETRY: "等待自动重试",
  NEEDS_AUTHOR: "需要作者处理",
  PAUSED: "已暂停",
  RUNNING: "正在创作",
  COMPLETED: "本次任务已完成",
  QUEUED: "等待开始",
};

export function recoveryLabel(run?: RunSummary | null) {
  if (!run) return "尚未开始创作";
  if (isBudgetPause(run)) return "已到本次预算上限";
  if (run.status === "PAUSED" && run.current_stage === "DIRECTION_SELECTION") return "等待作者确认方向";
  const state = recoveryState(run);
  if (state === "PAUSED" && run.pause_reason === "WINDOW_COMPLETE") return "本窗口已完成";
  return state ? recoveryLabels[state] : "状态待同步";
}

export function recoveryTime(value?: string | null) {
  if (!value) return undefined;
  // Existing SQLite timestamps are UTC with no suffix.
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? undefined : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
