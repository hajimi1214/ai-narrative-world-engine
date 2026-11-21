"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, CircleDot, Clock3, FileText, Layers3, LoaderCircle, Radio, RefreshCw, X } from "lucide-react";
import { useProjectLive, type Trace } from "./use-project-live";

const stageLabels: Record<string, string> = { WRITER: "正文写作", CRITIC: "章节审核", REPAIR: "问题修复", DIRECTOR: "导演规划", CHARACTER_ACTOR: "角色行动", WORLD_RESOLVER: "世界裁定", SCENE_COMMIT: "保存场景", AUTONOMOUS_LOOP: "自动推进", FRAMING: "整书方向", CAST_PREPARATION: "人物准备", WORLD_BUILDING: "世界设定", STORY_MACRO: "故事主线", VOLUME_PLANNING: "卷纲规划", CHAPTER_PLANNING: "章节规划", CHAPTER_DETAIL: "章节任务", CHAPTER_EXECUTION: "正文写作", QUALITY_REVIEW: "质量审核", QUALITY_REPAIR: "问题修复", AUTHOR_ADOPTION: "采用正文", AUTHOR_WINDOW_PLANNING: "当前窗口规划" };
const statusLabels: Record<string, string> = { STARTED: "进行中", RUNNING: "进行中", PENDING: "等待开始", SUCCEEDED: "已完成", FAILED: "失败", BLOCKED: "已暂停", PAUSED: "已暂停" };
const stageText = (value?: string, sourceType?: string | null) => value === "DIRECTOR" && sourceType === "STORY_PLAN" ? "整书规划" : stageLabels[value || ""] || value || "AI 任务";
const statusText = (value?: string) => statusLabels[value || ""] || "等待中";
const timestamp = (value?: string | null) => {
  if (!value) return NaN;
  // Persisted execution timestamps use UTC without a suffix; live SSE uses Z.
  return Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
};
const timeText = (value?: string | number | null) => {
  const parsed = typeof value === "number" ? value : timestamp(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "尚未同步";
};
const elapsedText = (trace: Trace) => {
  if (trace.latency_ms != null) return `${Math.max(0, Math.round(trace.latency_ms / 1000))} 秒`;
  const start = timestamp(trace.created_at);
  return trace.status === "STARTED" && Number.isFinite(start) ? `${Math.max(0, Math.floor((Date.now() - start) / 1000))} 秒` : "—";
};

export function AiLivePanel({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const { execution, autoRuns, liveSessions, sync, streamState, lastStreamAt, refresh } = useProjectLive(projectId);
  const traces = useMemo(() => {
    const autoTraces: Trace[] = autoRuns.flatMap((run) => (run.steps || []).map((step) => ({
      id: `auto-${step.id}`, stage: step.stage,
      status: step.status === "COMMITTED" ? "SUCCEEDED" : step.status === "FAILED" || step.status === "BLOCKED" ? step.status : run.status === "RUNNING" && step.stage === run.current_stage ? "STARTED" : step.status,
      model: step.usage_metrics?.model, latency_ms: step.usage_metrics?.latency_ms,
      error_code: step.error_code, created_at: step.started_at || run.updated_at,
      completed_at: step.completed_at, source_type: "AUTO_DIRECTOR",
    })));
    return [...execution, ...autoTraces].sort((a, b) => (timestamp(b.created_at) || 0) - (timestamp(a.created_at) || 0)).slice(0, 24);
  }, [execution, autoRuns]);
  const active = traces.some((trace) => trace.status === "STARTED") || liveSessions.some((session) => session.status === "RUNNING");
  const lastUpdatedAt = Math.max(sync.lastUpdatedAt || 0, lastStreamAt || 0) || null;
  const syncLabel = sync.status === "paused" ? "后台已暂停" : sync.status === "offline" ? "连接暂断" : sync.status === "syncing" ? "同步中" : "已同步";

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : triggerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); return; }
      if (event.key !== "Tab") return;
      const focusable = Array.from(panelRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], [tabindex='0']") || []);
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); panelRef.current?.focus(); }
      else if (!panelRef.current?.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [open]);

  return <>
    <button ref={triggerRef} className="ai-live-trigger" onClick={() => setOpen(true)} title="查看 AI 创作实况" aria-haspopup="dialog" aria-expanded={open} aria-controls={panelId}>
      <Radio size={15} className={active && sync.status !== "offline" ? "ai-live-pulse" : ""} aria-hidden="true" />
      <span>AI 实况</span><span className="ai-live-trigger-label">{sync.status === "offline" ? "连接暂断" : sync.status === "syncing" && !lastUpdatedAt ? "同步中" : active ? "正在创作" : "待命"}</span>
    </button>
    {open && <>
      <button className="ai-live-backdrop" tabIndex={-1} aria-label="关闭 AI 实况面板" onClick={() => setOpen(false)} />
      <aside ref={panelRef} id={panelId} className="ai-live-panel" role="dialog" aria-modal="true" aria-labelledby={`${panelId}-title`} tabIndex={-1}>
        <header>
          <div><div className="ai-live-title" id={`${panelId}-title`}><Radio size={15} aria-hidden="true" />AI 创作实况</div><small>{sync.status === "offline" ? "显示上次同步的工作状态" : active ? "创作任务正在执行" : "调用历史与实时阶段"}</small></div>
          <div className="ai-live-actions">
            <button onClick={() => setCollapsed((value) => !value)} title={collapsed ? "展开记录" : "收起记录"} aria-label={collapsed ? "展开创作记录" : "收起创作记录"} aria-expanded={!collapsed} aria-controls={`${panelId}-records`}><ChevronDown size={16} className={collapsed ? "ai-live-chevron-collapsed" : ""} aria-hidden="true" /></button>
            <button ref={closeRef} onClick={() => setOpen(false)} title="关闭" aria-label="关闭 AI 创作实况"><X size={16} aria-hidden="true" /></button>
          </div>
        </header>
        <div className={`ai-live-sync ai-live-sync-${sync.status}`}>
          <span role="status" aria-live="polite">{sync.status === "syncing" ? <LoaderCircle size={13} className="ai-live-spin" aria-hidden="true" /> : sync.status === "offline" ? <AlertTriangle size={13} aria-hidden="true" /> : <CircleDot size={13} aria-hidden="true" />}{syncLabel}</span>
          <span className="ai-live-sync-meta">上次更新 <time>{timeText(lastUpdatedAt)}</time></span>
          <button className="ai-live-refresh" onClick={refresh} disabled={sync.status === "syncing" || sync.status === "paused"} aria-label={sync.status === "offline" ? "立即重试同步创作记录" : "刷新创作记录"} title="刷新记录"><RefreshCw size={13} className={sync.status === "syncing" ? "ai-live-spin" : ""} aria-hidden="true" /></button>
        </div>
        {sync.status === "offline" && <div className="ai-live-connection-note" role="status">暂时无法更新全部记录，已保留上次数据。{sync.retryAt ? `将在 ${timeText(sync.retryAt)} 自动重试，也可点击刷新。` : "可点击刷新重试。"}</div>}
        {streamState === "recovering" && sync.status !== "offline" && <div className="ai-live-connection-note">实时推送正在重连，记录通过定时同步继续更新。</div>}
        {!collapsed && <div id={`${panelId}-records`} className="ai-live-list" aria-busy={!lastUpdatedAt && sync.status === "syncing"}>
          {liveSessions.map((session) => {
            const start = timestamp(session.started_at);
            const end = session.status === "RUNNING" ? Date.now() : timestamp(session.completed_at || session.updated_at);
            const seconds = Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, Math.floor((end - start) / 1000)) : null;
            return <article key={session.id} className={`ai-live-call ai-live-${session.status.toLowerCase()}`}>
              <div className="ai-live-call-head"><span className="ai-live-status">{session.status === "RUNNING" ? <LoaderCircle size={16} className="ai-live-spin" aria-hidden="true" /> : session.status === "COMPLETED" ? <CheckCircle2 size={16} aria-hidden="true" /> : <AlertTriangle size={16} aria-hidden="true" />}</span><div><strong>{session.label}</strong><p>{session.message}</p></div><time dateTime={session.updated_at}>{timeText(session.updated_at)}</time></div>
              <div className="ai-live-meta"><span><Layers3 size={12} aria-hidden="true" />{session.phase}</span><span><Clock3 size={12} aria-hidden="true" />{seconds === null ? "—" : `${seconds} 秒`}</span><span><FileText size={12} aria-hidden="true" />{session.total_chars} 字符</span></div>
              {session.preview && <><div className="ai-live-connection-note">即时预览，尚未完成校验</div><pre className="ai-live-preview">{session.preview}</pre></>}
              {session.status === "FAILED" && <div className="ai-live-note">需要检查：{session.error_code || "模型返回失败"}</div>}
            </article>;
          })}
          {traces.map((trace) => <article key={trace.id} className={`ai-live-call ai-live-${String(trace.status || "").toLowerCase()}`}>
            <div className="ai-live-call-head"><span className="ai-live-status">{trace.status === "STARTED" ? <LoaderCircle size={16} className="ai-live-spin" aria-hidden="true" /> : trace.status === "SUCCEEDED" ? <CheckCircle2 size={16} aria-hidden="true" /> : trace.status === "FAILED" || trace.status === "BLOCKED" ? <AlertTriangle size={16} aria-hidden="true" /> : <CircleDot size={16} aria-hidden="true" />}</span><div><strong>{stageText(trace.stage, trace.source_type)}</strong><p>{trace.status === "FAILED" ? `需要检查：${trace.error_code || "模型返回失败"}` : trace.status === "BLOCKED" ? "已暂停，等待作者处理" : trace.validation_report?.live_message || (trace.status === "SUCCEEDED" ? "调用已完成" : statusText(trace.status))}</p></div><time>{timeText(trace.created_at)}</time></div>
            <div className="ai-live-meta"><span><Layers3 size={12} aria-hidden="true" />{trace.validation_report?.live_phase || statusText(trace.status)}</span><span><Clock3 size={12} aria-hidden="true" />{elapsedText(trace)}</span><span><FileText size={12} aria-hidden="true" />{trace.model || "本地整理"}</span></div>
            {trace.validation_report?.generation_warning && <div className="ai-live-note">已保留作者大纲，模型部分可稍后深化</div>}
          </article>)}
          {!traces.length && !liveSessions.length && <div className="ai-live-empty"><Radio size={20} aria-hidden="true" /><p>{sync.status === "offline" ? "暂时无法读取创作记录" : !lastUpdatedAt ? "正在读取创作记录…" : "还没有 AI 工作记录"}<br /><small>{sync.status === "offline" ? "连接恢复后会自动更新，也可以立即重试。" : "开始生成规划或章节后，这里会自动出现。"}</small></p></div>}
        </div>}
      </aside>
    </>}
  </>;
}
