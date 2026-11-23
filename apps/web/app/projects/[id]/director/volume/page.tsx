"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, CircleDot, LoaderCircle, Lock, Pause, Play, RotateCcw, Send, UserRoundCog } from "lucide-react";
import { api, ApiError } from "../../../../lib";
import { recoveryState, type RunSummary } from "../../../../run-recovery";
import { ErrorState, PageHeader, SectionCard, StatusBadge } from "../../../../../components/ui/primitives";
import { createLivePoller, type LiveSyncState } from "../../../../../components/live/live-poller";
import { RunRecoveryPanel } from "../../../../../components/live/run-recovery-panel";

type Volume = { id: string; volume_number: number; title?: string; status: string; volume_goal?: string; core_conflict?: string; progress?: Record<string, any>; windows?: Array<Record<string, any>> };
type Run = RunSummary & { status: string; context?: { volume_id?: string; adopted_this_run?: number; adopted_chapters?: number; processed_this_run?: number }; settings?: Record<string, any>; token_usage?: { total_tokens?: number }; usage_summary?: { chapter?: Record<string, any>; window?: Record<string, any>; book?: Record<string, any> } };
export default function VolumeDirectorPage({ params, searchParams }: { params: { id: string }; searchParams?: { volume?: string | string[]; run_id?: string | string[] } }) {
  const requestedRunId = typeof searchParams?.run_id === "string" ? searchParams.run_id : searchParams?.run_id?.[0];
  const requestedVolumeId = typeof searchParams?.volume === "string" ? searchParams.volume : searchParams?.volume?.[0];
  const [volume, setVolume] = useState<Volume>(); const [run, setRun] = useState<Run>(); const [note, setNote] = useState(""); const [tokenBudgetIncrease, setTokenBudgetIncrease] = useState(""); const [message, setMessage] = useState(""); const [error, setError] = useState(""); const [actionError, setActionError] = useState(""); const [busy, setBusy] = useState(false); const [loaded, setLoaded] = useState(false);
  const pollerRef = useRef<ReturnType<typeof createLivePoller> | null>(null);
  const actionRevision = useRef(0);
  const [sync, setSync] = useState<LiveSyncState>({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 });
  useEffect(() => {
    let cancelled = false;
    const runsEndpoint = `/projects/${params.id}/author-guided-volume/runs`;
    setVolume(undefined);
    setRun(undefined);
    setLoaded(false);
    setError("");
    setSync({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 });
    const poller = createLivePoller({
      intervalMs: 2000,
      load: async (signal) => {
        const revision = actionRevision.current;
        const [volumeValue, runValue] = await Promise.all([
          api(`/projects/${params.id}/volumes`, { signal }) as Promise<Volume[]>,
          (requestedRunId ? api(`${runsEndpoint}/${encodeURIComponent(requestedRunId)}`, { signal }) : api(runsEndpoint, { signal }))
            .catch((reason: unknown) => { if (requestedRunId && reason instanceof ApiError && reason.status === 404) return null; throw reason; }) as Promise<Run | Run[] | null>,
        ]);
        if (signal.aborted || cancelled || revision !== actionRevision.current) return;
        if (!Array.isArray(volumeValue) || (!requestedRunId && !Array.isArray(runValue))) throw new Error("INVALID_VOLUME_DIRECTOR_RESPONSE");
        const volumes = [...volumeValue].sort((left, right) => right.volume_number - left.volume_number);
        const exactRun = requestedRunId && !Array.isArray(runValue) && runValue?.id === requestedRunId ? runValue : undefined;
        const selected = requestedRunId
          ? volumes.find((item) => item.id === exactRun?.context?.volume_id && (!requestedVolumeId || item.id === requestedVolumeId))
          : requestedVolumeId
            ? volumes.find((item) => item.id === requestedVolumeId)
            : volumes.find((item) => ["ACTIVE", "EXTENDING", "READY_TO_SEAL", "BLOCKED"].includes(item.status)) || volumes.find((item) => item.status === "DRAFT") || volumes[0];
        setVolume(selected);
        setRun(selected ? exactRun || (Array.isArray(runValue) ? runValue.find((item) => item.context?.volume_id === selected.id) : undefined) : undefined);
        setError(requestedRunId && !selected ? "找不到这条卷创作运行。请从书库或实况打开该任务。" : requestedVolumeId && !selected ? "这卷不存在，请从卷总览重新选择。" : "");
        setLoaded(true);
      },
      onState: (state) => { if (!cancelled) setSync(state); },
    });
    pollerRef.current = poller;
    const updateVisibility = () => poller.setPaused(document.hidden);
    document.addEventListener("visibilitychange", updateVisibility);
    updateVisibility();
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", updateVisibility);
      poller.dispose();
      if (pollerRef.current === poller) pollerRef.current = null;
    };
  }, [params.id, requestedVolumeId, requestedRunId]);
  useEffect(() => { setNote(""); setMessage(""); setActionError(""); }, [volume?.id]);
  useEffect(() => { setTokenBudgetIncrease(""); }, [run?.id, run?.pause_reason]);
  const retrySync = () => { setError(""); setActionError(""); pollerRef.current?.refresh(); };
  const syncLabel = sync.status === "syncing" ? "同步中" : sync.status === "offline" ? "API 连接暂断" : sync.status === "paused" ? "后台暂停同步" : "已同步";
  const lastSync = sync.lastUpdatedAt ? new Date(sync.lastUpdatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "尚未同步";
  const retryTime = sync.retryAt ? new Date(sync.retryAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
  function setOperationFailure(reason: unknown, fallback: string) {
    setActionError(reason instanceof ApiError ? reason.message : fallback);
  }
  async function action(path: string, body?: unknown) {
    if (!volume || (requestedRunId && run?.id !== requestedRunId)) return false;
    const runPath = requestedRunId && (path === "continue" || path === "pause");
    const endpoint = runPath
      ? `/projects/${params.id}/author-guided-volume/runs/${encodeURIComponent(requestedRunId)}/${path}`
      : `/projects/${params.id}/volumes/${volume.id}/${path}`;
    actionRevision.current += 1;
    setBusy(true); setActionError(""); setError(""); setMessage("");
    try {
      await api(endpoint, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      const raisedBudget = path === "continue" && typeof body === "object" && body !== null && "operational_token_budget" in body;
      setMessage(raisedBudget ? "Token 预算已提高，任务将从保存的检查点继续。" : path === "pause" ? "当前运行已暂停。" : path === "continue" ? "任务将从保存的检查点继续。" : path === "seal" ? "本卷已封存，连续性快照已保存。" : path === "unseal" ? "本卷已解封。" : "操作已保存。");
      return true;
    } catch (reason) { setOperationFailure(reason, "操作失败"); return false; }
    finally { actionRevision.current += 1; pollerRef.current?.refresh(); setBusy(false); }
  }
  async function runAction(path: "retry" | "takeover") {
    if (!run || (requestedRunId && run.id !== requestedRunId)) return;
    actionRevision.current += 1;
    setBusy(true); setActionError(""); setError(""); setMessage("");
    try {
      await api(`/projects/${params.id}/author-guided-volume/runs/${run.id}/${path}`, { method: "POST" });
      setMessage(path === "retry" ? "已加入重试队列，任务将从保存的检查点继续。" : "运行已交由作者接管。可去章节工作台编辑已保存的正文。");
    } catch (reason) { setOperationFailure(reason, "运行操作失败"); }
    finally { actionRevision.current += 1; pollerRef.current?.refresh(); setBusy(false); }
  }
  async function guidance() { if (!volume || !note.trim()) return; if (await action("guidance", { author_note: note, affected_scope: "WINDOW", requires_replan: false })) setNote(""); }
  if (!volume) return <main className="stack"><PageHeader title="当前卷导演" description={loaded ? "尚未找到可展示的卷。" : sync.status === "offline" ? "暂时无法读取卷级检查点。" : "正在读取卷级检查点。"} /><SyncStatus status={sync.status} label={syncLabel} lastSync={lastSync} retryTime={retryTime} refresh={retrySync} /><div className="sr-only" role="status" aria-live="polite">{lastSync} · 同步失败 {sync.failures} 次</div>{error && <ErrorState message={error} retry={retrySync} />}{sync.status === "offline" && <ErrorState message={`卷状态同步失败；保留上次显示的数据。${retryTime ? `将在 ${retryTime} 自动重试，` : ""}可以现在重试。`} retry={retrySync} />}{loaded && sync.status === "ready" && !volume && <p className="muted">当前还没有可展示的卷。</p>}<a className="button secondary" href={`/projects/${params.id}/volumes`}>返回卷总览</a></main>;
  const sealed = volume.status === "SEALED";
  const state = run && recoveryState(run);
  const canOperate = !busy && sync.status !== "offline" && (!requestedRunId || run?.id === requestedRunId);
  const canPause = canOperate && !sealed && (state === "RUNNING" || state === "QUEUED" || state === "WAITING_RETRY");
  const canContinue = canOperate && !sealed && (!run || state === "PAUSED");
  const canRetry = canOperate && !sealed && state === "NEEDS_AUTHOR" && run?.pause_reason !== "MAX_RETRIES_REACHED";
  const canTakeover = canOperate && !sealed && Boolean(run) && state !== "COMPLETED";
  const progress = volume.progress || {};
  const usage = run?.usage_summary?.window || {};
  const confirmedTokens = Math.max(Number(run?.token_usage?.total_tokens || 0), Number(run?.usage_summary?.book?.total_tokens || 0));
  const reservedTokens = Number(run?.recovery?.reserved_tokens || 0);
  const usedTokens = confirmedTokens + reservedTokens;
  const tokenBudget = Number(run?.settings?.operational_token_budget || run?.settings?.max_tokens || 0);
  const processedThisRun = run?.context?.processed_this_run ?? run?.context?.adopted_this_run ?? 0;
  const chapterBudget = Number(run?.settings?.operational_run_chapter_budget || run?.settings?.max_chapters || 0);
  const pauseMessage = run?.pause_reason === "TOKEN_BUDGET_EXCEEDED"
    ? `本次 token 上限 ${tokenBudget.toLocaleString()} 已用完。提高上限后会从当前检查点继续，已完成章节会保留。`
    : run?.pause_reason === "RUN_CHAPTER_BUDGET_REACHED"
      ? `本次已处理 ${processedThisRun} / ${chapterBudget} 章。继续后从下一章接着写。`
      : run?.pause_reason === "WINDOW_COMPLETE"
        ? "当前章节窗口已完成。继续后会从下一批未写章节接着写；封卷仍需你确认。"
        : run?.pause_reason === "MAX_RETRIES_REACHED"
          ? "当前检查点的自动重试次数已用完。请接管并检查失败阶段后再继续。"
          : run?.next_action || run?.pause_reason || "等待作者开始当前卷。";
  return <main className="stack"><PageHeader title={`第 ${volume.volume_number} 卷导演台`} description="作者掌握书籍合同、剧情走向和封卷决定；预算内连续推进写作、审核与采用。" action={<StatusBadge value={volume.status} />} /><SyncStatus status={sync.status} label={syncLabel} lastSync={lastSync} retryTime={retryTime} refresh={retrySync} />{error && <ErrorState message={error} retry={retrySync} />}{actionError && <ErrorState message={actionError} retry={retrySync} />}{sync.status === "offline" && <div className="preview-warning" role="status"><AlertTriangle size={16} /><span>卷状态同步失败，保留最近一次数据。{retryTime ? `将在 ${retryTime} 自动重试；` : ""}可手动重试。</span><button className="secondary" onClick={retrySync}>立即重试</button></div>}{message && <div className="success-state" role="status">{message}</div>}<SectionCard title="运行状态"><div className="run-metrics"><span>已处理 {processedThisRun}{chapterBudget ? ` / ${chapterBudget}` : ""} 章</span><span>窗口 token：{usage.total_tokens || 0}</span><span>调用次数：{usage.calls || 0}</span><span>成本：{usage.cost_status === "UNKNOWN" ? "UNKNOWN" : usage.estimated_cost}</span></div>{run ? <RunRecoveryPanel run={run} nextAction={pauseMessage} /> : <p className="muted">等待作者开始当前卷。</p>}<div className="row"><button className="secondary" onClick={() => void runAction("retry")} disabled={!canRetry}><RotateCcw size={15} />重试</button><button className="secondary" onClick={() => void runAction("takeover")} disabled={!canTakeover}><UserRoundCog size={15} />接管</button><Link className="button secondary" href={`/projects/${params.id}/chapters`}>章节工作台</Link></div></SectionCard><SectionCard title="当前卷合同"><div className="task-summary"><div><small>卷目标</small><p>{volume.volume_goal || "未设置"}</p></div><div><small>核心冲突</small><p>{volume.core_conflict || "未设置"}</p></div><div><small>目标进度</small><p>{Math.round((progress.volume_goal_progress || 0) * 100)}%</p></div><div><small>完成判断</small><p>{progress.should_prepare_seal ? "可以提出封存建议" : "尚未满足封存条件，继续当前卷"}</p></div></div></SectionCard><SectionCard title="当前章节窗口" description="窗口最多 10 章，结束不等于本卷结束。"><div className="ai-live-sync" role="status" aria-live="polite"><span>运行状态：{run?.status || "未启动"}</span><span className="ai-live-sync-meta">本次已处理 {processedThisRun} / {chapterBudget || "-"} 章 · 本次 token {usedTokens.toLocaleString()} / {tokenBudget ? tokenBudget.toLocaleString() : "不限"} · 累计采用 {run?.context?.adopted_chapters || 0} 章</span></div>{(volume.windows || []).map((window) => <div className="section-heading" key={window.id}><span>第 {window.start_chapter_number}-{window.end_chapter_number} 章 · {window.status}</span><span className="muted">已生成 {window.actual_generated_count || 0} 章</span></div>)}{run?.pause_reason === "TOKEN_BUDGET_EXCEEDED" ? <div className="form-grid token-budget-control"><label className="field"><span>提高本次 token 上限 <small>当前 {tokenBudget.toLocaleString()} · 已用 {usedTokens.toLocaleString()}</small></span><input type="number" min={Math.max(tokenBudget, usedTokens) + 1} max={10000000} step={1000} value={tokenBudgetIncrease} onChange={(event) => setTokenBudgetIncrease(event.target.value)} placeholder={String(Math.max(tokenBudget * 2, usedTokens + 10000, 10000))} /></label><button onClick={() => void action("continue", { operational_token_budget: Number(tokenBudgetIncrease) })} disabled={!canContinue || !Number.isInteger(Number(tokenBudgetIncrease)) || Number(tokenBudgetIncrease) <= Math.max(tokenBudget, usedTokens) || Number(tokenBudgetIncrease) > 10000000}><Play size={15} />提高预算并从检查点继续</button></div> : <div className="row"><button onClick={() => void action("continue")} disabled={!canContinue}><Play size={15} />继续</button><button className="secondary" onClick={() => void action("pause", { reason: "作者暂停当前卷" })} disabled={!canPause}><Pause size={15} />暂停</button></div>}</SectionCard><SectionCard title="作者指导"><textarea className="field full" disabled={sealed || !canOperate} value={note} onChange={(event) => setNote(event.target.value)} placeholder="告诉 AI 当前窗口要强调什么；已采用正文和封存卷不会被重写。" /><div className="row"><button onClick={() => void guidance()} disabled={!canOperate || sealed || !note.trim()}><Send size={15} />提交指导</button><button className="secondary" onClick={() => void action("seal", { author_confirmed: true })} disabled={!canOperate || sealed || !progress.should_prepare_seal}><Lock size={15} />确认封存本卷</button><button className="secondary" onClick={() => void action("unseal", { reason: "作者要求重新规划" })} disabled={!canOperate || volume.status !== "SEALED"}><RotateCcw size={15} />解封</button></div></SectionCard></main>;
}

function SyncStatus({ status, label, lastSync, retryTime, refresh }: { status: LiveSyncState["status"]; label: string; lastSync: string; retryTime: string; refresh: () => void }) {
  return <div className={`ai-live-sync${status === "offline" ? " ai-live-sync-offline" : ""}`} role="status" aria-live="polite"><span>{status === "syncing" ? <LoaderCircle size={13} className="ai-live-spin" /> : status === "offline" ? <AlertTriangle size={13} /> : <CircleDot size={13} />}{label}</span><span className="ai-live-sync-meta">最近更新：{lastSync}{status === "offline" && retryTime ? ` · 下次重试 ${retryTime}` : ""}</span><button className="ai-live-refresh" onClick={refresh} disabled={status === "paused" || status === "syncing"} aria-label={status === "offline" ? "立即重试同步当前卷运行状态" : "刷新当前卷运行状态"}>{status === "offline" ? "立即重试" : "刷新"}</button></div>;
}
