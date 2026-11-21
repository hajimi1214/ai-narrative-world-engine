import { recoveryLabel, recoveryState, recoveryTime, type RunSummary } from "../../app/run-recovery";

const checkpointLabels: Record<string, string> = {
  IDEA: "灵感准备", FRAMING: "整书方向", BOOK_CONTRACT: "书籍合同",
  CAST_PREPARATION: "人物准备", WORLD_BUILDING: "世界设定", STORY_MACRO: "故事主线",
  VOLUME_PLANNING: "卷纲规划", VOLUME_ACTIVE: "本卷推进", VOLUME_OUTLINE: "卷纲",
  CHAPTER_PLANNING: "章节规划", CHAPTER_DETAIL: "章节任务", CHAPTER_WINDOW_PLANNING: "当前窗口规划",
  AUTHOR_WINDOW_PLANNING: "当前窗口规划", CHAPTER_WINDOW_EXECUTION: "当前窗口写作",
  CHAPTER_EXECUTION: "正文写作", QUALITY_REVIEW: "质量审核", QUALITY_REPAIR: "正文修复",
  AUTHOR_ADOPTION: "采用正文", NEXT_CHAPTER: "推进下一章", COMPLETED: "本次创作结束",
};

export function RunRecoveryPanel({ run, nextAction }: { run: RunSummary; nextAction?: string | null }) {
  const state = recoveryState(run);
  const recovery = run.recovery;
  const checkpoint = recovery?.last_checkpoint;
  const checkpointTime = recoveryTime(checkpoint?.saved_at);
  const retryTime = state === "WAITING_RETRY" ? recoveryTime(recovery?.next_retry_at) : undefined;
  const action = nextAction || run.next_action;

  return <div className="run-recovery">
    <p className="run-recovery-state" role="status">{recoveryLabel(run)}</p>
    <p className="muted">最近保存的检查点：{checkpoint ? <>{checkpointLabels[checkpoint.stage] || "创作进度已保存"}{checkpointTime && <> · <time dateTime={checkpoint.saved_at}>{checkpointTime}</time></>}</> : "尚未保存"}</p>
    {state === "WAITING_RETRY" && <p>预计重试：{retryTime && recovery?.next_retry_at ? <time dateTime={recovery.next_retry_at}>{retryTime}</time> : "等待本地服务调度"}{Boolean(recovery?.max_attempts) && <> · 已尝试 {recovery?.attempts} / {recovery?.max_attempts} 次</>}</p>}
    {action && <p>{action}</p>}
    {recovery?.usage_uncertain && <p className="muted">上一次请求的用量待核对{recovery.reserved_tokens ? `，当前预算已预留 ${recovery.reserved_tokens.toLocaleString()} tokens` : ""}，可到模型服务商查看账单。</p>}
    {(state === "RUNNING" || state === "QUEUED" || state === "WAITING_RETRY") && <p className="muted">本地服务运行时，关闭浏览器也会继续。重新打开此页面可查看进度。</p>}
  </div>;
}
