import {
  recoveryLabel,
  recoveryState,
  recoveryTime,
  runBudgetSummary,
  type RunSummary,
} from "../../app/run-recovery";

const checkpointLabels: Record<string, string> = {
  IDEA: "灵感准备",
  FRAMING: "整书方向",
  BOOK_CONTRACT: "书籍合同",
  CAST_PREPARATION: "人物准备",
  WORLD_BUILDING: "世界设定",
  STORY_MACRO: "故事主线",
  VOLUME_PLANNING: "卷纲规划",
  VOLUME_ACTIVE: "本卷推进",
  VOLUME_OUTLINE: "卷纲",
  CHAPTER_PLANNING: "章节规划",
  CHAPTER_DETAIL: "章节任务",
  CHAPTER_WINDOW_PLANNING: "当前窗口规划",
  AUTHOR_WINDOW_PLANNING: "当前窗口规划",
  CHAPTER_WINDOW_EXECUTION: "当前窗口写作",
  CHAPTER_EXECUTION: "正文写作",
  QUALITY_REVIEW: "质量审核",
  QUALITY_REPAIR: "正文修复",
  AUTHOR_ADOPTION: "采用正文",
  NEXT_CHAPTER: "推进下一章",
  COMPLETED: "本次创作结束",
  VOLUME_SKELETON: "卷规划",
  VOLUME_PROGRESS_ASSESSMENT: "封卷检查",
  VOLUME_CLOSING: "卷收束",
  VOLUME_SEALED: "卷已封存",
  NEXT_VOLUME_READY: "准备下一卷",
  BOOK_COMPLETION_PROPOSED: "整本完成审核",
  BOOK_COMPLETED: "整本已完成",
  DIRECTION_SELECTION: "等待作者确认方向",
  PAUSED: "已暂停",
  BLOCKED: "等待作者处理",
  FAILED: "运行失败",
};

const branchStatusLabels: Record<string, string> = {
  OPEN: "推演中",
  READY: "等待审核与采用",
  ADOPTED: "已随正文采用",
  STALE: "已过期",
  ABORTED: "已中止",
};

export function RunRecoveryPanel({
  run,
  nextAction,
}: {
  run: RunSummary;
  nextAction?: string | null;
}) {
  const state = recoveryState(run);
  const recovery = run.recovery;
  const checkpoint = recovery?.last_checkpoint;
  const checkpointTime = recoveryTime(checkpoint?.saved_at);
  const retryTime =
    state === "WAITING_RETRY"
      ? recoveryTime(recovery?.next_retry_at)
      : undefined;
  const action = nextAction || run.next_action;
  const context = run.context || {};
  const stage =
    state === "NEEDS_AUTHOR" || state === "WAITING_RETRY"
      ? String(
          context.worker_stage ||
            context.failed_stage ||
            checkpoint?.stage ||
            run.current_stage ||
            "",
        )
      : state === "PAUSED" && run.current_stage === "PAUSED"
        ? String(context.resume_stage || checkpoint?.stage || "")
        : String(run.current_stage || "");
  const stageHeading =
    state === "NEEDS_AUTHOR"
      ? "需要处理阶段"
      : state === "PAUSED" && run.current_stage === "PAUSED"
        ? "恢复阶段"
        : "当前阶段";
  const budget = runBudgetSummary(run);
  const usage = run.token_usage || {};
  const unknownUsageCalls = Math.max(0, Math.floor(Number(usage.unknown_usage_calls || 0)));
  const branchStatus = String(context.chapter_world_branch_status || "");
  const branchId = String(context.chapter_world_branch_id || "");
  const branchChapter =
    branchStatus === "ADOPTED"
      ? context.last_adopted_chapter_number || context.current_chapter_number
      : context.current_chapter_number || context.last_processed_chapter_number;

  return (
    <div className="run-recovery">
      <p className="run-recovery-state" role="status">
        {recoveryLabel(run)}
      </p>
      {stage && (
        <p>
          {stageHeading}：{checkpointLabels[stage] || stage}
        </p>
      )}
      <p className="muted">
        最近保存的检查点：
        {checkpoint ? (
          <>
            {checkpointLabels[checkpoint.stage] || "创作进度已保存"}
            {checkpointTime && (
              <>
                {" "}
                · <time dateTime={checkpoint.saved_at}>{checkpointTime}</time>
              </>
            )}
          </>
        ) : (
          "尚未保存"
        )}
      </p>
      {branchId && (
        <p>
          章节分支：{branchChapter ? `第 ${branchChapter} 章 · ` : ""}
          {branchStatusLabels[branchStatus] || "已保存"} ·{" "}
          {branchId.slice(0, 8)}
        </p>
      )}
      {(budget.chapters_remaining !== null ||
        budget.tokens_remaining !== null) && (
        <div className="run-metrics run-recovery-budgets">
          {budget.chapters_remaining !== null && (
            <span>
              章节预算余量：{budget.chapters_remaining} /{" "}
              {budget.chapter_budget} 章
            </span>
          )}
          {budget.tokens_remaining !== null && (
            <span>
              Token 预算余量：{budget.tokens_remaining.toLocaleString()} /{" "}
              {budget.token_budget?.toLocaleString()} · 已用{" "}
              {budget.tokens_used.toLocaleString()}
              {budget.tokens_reserved
                ? `，故障预留 ${budget.tokens_reserved.toLocaleString()}`
                : ""}
            </span>
          )}
        </div>
      )}
      {state === "WAITING_RETRY" && (
        <p>
          预计重试：
          {retryTime && recovery?.next_retry_at ? (
            <time dateTime={recovery.next_retry_at}>{retryTime}</time>
          ) : (
            "等待本地服务调度"
          )}
          {Boolean(recovery?.max_attempts) && (
            <>
              {" "}
              · 已尝试 {recovery?.attempts} / {recovery?.max_attempts} 次
            </>
          )}
        </p>
      )}
      {action && <p>{action}</p>}
      {recovery?.usage_uncertain && (
        <p className="muted">
          上一次请求的用量待核对{unknownUsageCalls ? `（${unknownUsageCalls} 次调用）` : ""}
          {recovery.reserved_tokens
            ? `，当前预算已预留 ${recovery.reserved_tokens.toLocaleString()} tokens`
            : ""}
          ，可到模型服务商查看账单。
        </p>
      )}
      {state === "QUEUED" && run.status === "RUNNING" && (
        <p className="muted">
          运行已保存在本地，等待工作线程接手。
          {checkpoint ? "接手后从最近检查点恢复。" : "接手后继续当前运行。"}
        </p>
      )}
      {(state === "RUNNING" || state === "WAITING_RETRY") && (
        <p className="muted">
          本地服务运行时，关闭浏览器也会继续。重新打开此页面可查看进度。
        </p>
      )}
    </div>
  );
}
