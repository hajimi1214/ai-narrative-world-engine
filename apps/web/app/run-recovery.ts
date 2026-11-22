export type RecoveryState =
  | "WAITING_RETRY"
  | "NEEDS_AUTHOR"
  | "PAUSED"
  | "RUNNING"
  | "COMPLETED"
  | "QUEUED";

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
  settings?: Record<string, unknown>;
  context?: Record<string, unknown>;
  token_usage?: Record<string, unknown>;
  usage_metrics?: Record<string, unknown>;
  usage_summary?: { book?: Record<string, unknown> };
};

export type RunBudgetSummary = {
  chapter_budget: number | null;
  chapters_remaining: number | null;
  token_budget: number | null;
  tokens_remaining: number | null;
  tokens_used: number;
  tokens_reserved: number;
};

export const directorHref = (
  projectId: string,
  run: Pick<RunSummary, "id" | "run_mode">,
) => {
  const director = run.run_mode === "AUTHOR_GUIDED_VOLUME" ? "volume" : "auto";
  return `/projects/${encodeURIComponent(projectId)}/director/${director}?${new URLSearchParams({ run_id: run.id })}`;
};

export function recoveryState(run: RunSummary): RecoveryState | null {
  if (run.status === "PAUSED" && run.current_stage === "DIRECTION_SELECTION")
    return "NEEDS_AUTHOR";
  if (run.recovery) return run.recovery.state;
  if (run.status === "CREATED") return "QUEUED";
  if (run.status === "FAILED" || run.status === "BLOCKED")
    return "NEEDS_AUTHOR";
  if (
    run.status === "PAUSED" ||
    run.status === "RUNNING" ||
    run.status === "COMPLETED"
  )
    return run.status;
  return null;
}

export const isBudgetPause = (run: RunSummary) =>
  run.status === "PAUSED" &&
  [
    "RUN_CHAPTER_BUDGET_REACHED",
    "TOKEN_BUDGET_EXCEEDED",
    "MODEL_CALL_BUDGET_EXCEEDED",
  ].includes(run.pause_reason || "");

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
  if (run.status === "PAUSED" && run.current_stage === "DIRECTION_SELECTION")
    return "等待作者确认方向";
  const state = recoveryState(run);
  if (state === "PAUSED" && run.pause_reason === "WINDOW_COMPLETE")
    return "本窗口已完成";
  if (state === "QUEUED" && run.status === "RUNNING") return "等待本地服务接手";
  return state ? recoveryLabels[state] : "状态待同步";
}

export function recoveryTime(value?: string | null) {
  if (!value) return undefined;
  // Existing SQLite timestamps are UTC with no suffix.
  const date = new Date(
    /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`,
  );
  return Number.isNaN(date.getTime())
    ? undefined
    : date.toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function positiveLimit(...values: unknown[]): number | null {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return Math.floor(number);
  }
  return null;
}

function nonNegativeCount(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : 0;
}

export function runBudgetSummary(run: RunSummary): RunBudgetSummary {
  const settings = record(run.settings);
  const context = record(run.context);
  const request = record(context.request);
  const lengthPolicy = record(request.length_policy);
  const chapterBudget = positiveLimit(
    settings.operational_run_chapter_budget,
    settings.max_chapters,
    request.operational_run_chapter_budget,
    request.max_chapters,
  );
  const completedChapters = Array.isArray(context.completed_chapters)
    ? context.completed_chapters.length
    : 0;
  const chaptersUsed = Math.max(
    nonNegativeCount(context.processed_this_run),
    nonNegativeCount(context.adopted_this_run),
    nonNegativeCount(context.generated_this_run),
    completedChapters,
  );

  const tokenBudget = positiveLimit(
    settings.operational_token_budget,
    settings.max_tokens,
    request.operational_token_budget,
    lengthPolicy.operational_token_budget,
  );
  const usage = record(run.token_usage);
  const metrics = record(run.usage_metrics);
  const bookUsage = record(run.usage_summary?.book);
  const tokensUsed = Math.max(
    nonNegativeCount(usage.total_tokens ?? usage.tokens),
    nonNegativeCount(metrics.total_tokens),
    nonNegativeCount(bookUsage.total_tokens),
  );
  const tokensReserved = nonNegativeCount(run.recovery?.reserved_tokens);

  return {
    chapter_budget: chapterBudget,
    chapters_remaining:
      chapterBudget === null ? null : Math.max(0, chapterBudget - chaptersUsed),
    token_budget: tokenBudget,
    tokens_remaining:
      tokenBudget === null
        ? null
        : Math.max(0, tokenBudget - tokensUsed - tokensReserved),
    tokens_used: tokensUsed,
    tokens_reserved: tokensReserved,
  };
}
