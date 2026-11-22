"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, CircleAlert, CircleDot, LoaderCircle, Pause, Play, RefreshCw, RotateCcw, UserRoundCog, Eye } from "lucide-react";
import { api, ApiError } from "../../../../lib";
import { directorHref, recoveryState, recoveryTime, type RunSummary } from "../../../../run-recovery";
import { createLivePoller, type LiveSyncState } from "../../../../../components/live/live-poller";
import { RunRecoveryPanel } from "../../../../../components/live/run-recovery-panel";

type UsageMetrics = { calls?: number; prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; latency_ms?: number; estimated_cost?: number | null; cost_status?: string; provider?: string; model?: string };
type Step = { id: string; stage: string; status: string; attempt: number; output_artifact_id?: string; output_payload?: Record<string, any>; error_code?: string; error_summary?: string; token_usage?: Record<string, any>; usage_metrics?: UsageMetrics };
type Run = RunSummary & { status: string; current_stage: string; current_chapter_id?: string; updated_at?: string; settings?: Record<string, any>; token_usage?: Record<string, any>; usage_metrics?: UsageMetrics; context?: Record<string, any>; steps: Step[] };

const stages = ["IDEA", "FRAMING", "CAST_PREPARATION", "WORLD_BUILDING", "STORY_MACRO", "VOLUME_PLANNING", "CHAPTER_PLANNING", "CHAPTER_DETAIL", "CHAPTER_EXECUTION", "QUALITY_REVIEW", "QUALITY_REPAIR", "AUTHOR_ADOPTION", "NEXT_CHAPTER", "COMPLETED"];
const labels: Record<string, string> = { IDEA: "灵感", FRAMING: "方向评分", CAST_PREPARATION: "角色准备", WORLD_BUILDING: "世界边界", STORY_MACRO: "故事主线", VOLUME_PLANNING: "卷纲", CHAPTER_PLANNING: "章节任务", CHAPTER_DETAIL: "章节详情", CHAPTER_EXECUTION: "章节正文", QUALITY_REVIEW: "质量审核", QUALITY_REPAIR: "自动修复", AUTHOR_ADOPTION: "自动采用", NEXT_CHAPTER: "下一章", COMPLETED: "整本完成", PAUSED: "已暂停", FAILED: "失败", BLOCKED: "已阻塞" };

export default function AutoDirectorPage({ params, searchParams }: { params: { id: string }; searchParams?: { run_id?: string | string[] } }) {
  const router = useRouter();
  const requestedRunId = typeof searchParams?.run_id === "string" ? searchParams.run_id : searchParams?.run_id?.[0];
  const [run, setRun] = useState<Run>(); const [draft, setDraft] = useState<Record<string, any>>(); const [quality, setQuality] = useState<Record<string, any>>(); const [task, setTask] = useState<Record<string, any>>(); const [chapters, setChapters] = useState<Array<Record<string, any>>>([]); const [directionOpen, setDirectionOpen] = useState(false); const [showStarter, setShowStarter] = useState(false); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [sync, setSync] = useState<LiveSyncState>({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 }); const pollerRef = useRef<ReturnType<typeof createLivePoller> | null>(null); const [start, setStart] = useState({ inspiration: "", target_chapters: 450, max_chapters: 10, target_words_per_chapter: 2800, tone: "" });
  const [loadError, setLoadError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const actionRevision = useRef(0);
  const draftGeneration = useRef(0);
  const chapterDetailGeneration = useRef(0);
  useEffect(() => {
    const endpoint = `/projects/${params.id}/auto-director/runs`;
    let followedRunId = requestedRunId;
    setRun(undefined);
    setChapters([]);
    setLoadError("");
    setLoaded(false);
    setError("");
    setShowStarter(false);
    setSync({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 });
    const poller = createLivePoller({
      intervalMs: 2000,
      onState: setSync,
      load: async (signal) => {
        const revision = actionRevision.current;
        const [result, items] = await Promise.all([
          (followedRunId ? api(`${endpoint}/${encodeURIComponent(followedRunId)}`, { signal }) : api(endpoint, { signal }))
            .catch((reason: unknown) => { if (requestedRunId && reason instanceof ApiError && reason.status === 404) return null; throw reason; }) as Promise<Run | Run[] | null>,
          api(`/projects/${params.id}/chapters`, { signal }) as Promise<Array<Record<string, any>>>,
        ]);
        if (signal.aborted || revision !== actionRevision.current) return;
        const latest = Array.isArray(result)
          ? result.filter((item) => item.run_mode !== "AUTHOR_GUIDED_VOLUME").sort((a, b) => String(b.updated_at || b.id).localeCompare(String(a.updated_at || a.id)))[0]
          : result?.run_mode !== "AUTHOR_GUIDED_VOLUME" && result?.id === followedRunId ? result ?? undefined : undefined;
        if (latest) followedRunId = latest.id;
        setRun(latest);
        setChapters(items.filter((item) => item.active !== false).sort((a, b) => Number(a.number || 0) - Number(b.number || 0)));
        setLoadError(requestedRunId && !latest ? "找不到这条自动导演运行。请从书库或实况打开该任务。" : "");
        setLoaded(true);
      },
    });
    pollerRef.current = poller;
    const visibility = () => poller.setPaused(document.hidden);
    visibility(); document.addEventListener("visibilitychange", visibility);
    return () => { poller.dispose(); if (pollerRef.current === poller) pollerRef.current = null; document.removeEventListener("visibilitychange", visibility); };
  }, [params.id, requestedRunId]);
  useEffect(() => { void api(`/projects/${params.id}`).then((project: any) => setStart((current) => ({ ...current, inspiration: project.story_seed || current.inspiration, target_words_per_chapter: project.target_chapter_words || current.target_words_per_chapter }))).catch(() => undefined); }, [params.id]);
  const displayedChapter = run?.status === "COMPLETED" ? (run.context?.last_adopted_chapter_number || run.context?.current_chapter_number || 1) : (run?.context?.current_chapter_number || 1);
  const draftTargetChapterId = run?.status === "COMPLETED" ? run.context?.last_adopted_chapter_id : run?.current_chapter_id;
  const latestDraftStep = draftTargetChapterId ? [...(run?.steps || [])].reverse().find((step) => step.stage === "CHAPTER_EXECUTION" && step.status === "COMMITTED" && step.output_payload?.chapter_id === draftTargetChapterId) : undefined;
  const latestDraftId = latestDraftStep?.output_artifact_id;
  useEffect(() => {
    const generation = ++draftGeneration.current;
    const controller = new AbortController();
    if (!latestDraftId) { setDraft(undefined); return () => controller.abort(); }
    setDraft(undefined);
    void api(`/projects/${params.id}/writer-drafts/${latestDraftId}`, { signal: controller.signal }).then((value: unknown) => {
      if (!controller.signal.aborted && generation === draftGeneration.current) setDraft(value as Record<string, any>);
    }).catch(() => undefined);
    return () => { controller.abort(); };
  }, [params.id, latestDraftId]);
  useEffect(() => {
    const chapterId = run?.current_chapter_id;
    const generation = ++chapterDetailGeneration.current;
    const controller = new AbortController();
    setQuality(undefined); setTask(undefined);
    if (!chapterId) return () => controller.abort();
    void Promise.allSettled([
      api(`/projects/${params.id}/chapters/${chapterId}/quality`, { signal: controller.signal }),
      api(`/projects/${params.id}/chapters/${chapterId}/planning-task`, { signal: controller.signal }),
    ]).then(([qualityResult, taskResult]) => {
      if (controller.signal.aborted || generation !== chapterDetailGeneration.current || run?.current_chapter_id !== chapterId) return;
      if (qualityResult.status === "fulfilled") setQuality(qualityResult.value as Record<string, any>);
      if (taskResult.status === "fulfilled") { const value = taskResult.value as any; setTask(value?.task || value); }
    });
    return () => { controller.abort(); };
  }, [params.id, run?.current_chapter_id, run?.current_stage]);
  async function action(name: string, body?: unknown) { if (!run || !canOperate) return; actionRevision.current += 1; setBusy(true); setError(""); try { const next = await api(`/projects/${params.id}/auto-director/runs/${run.id}/${name}`, { method: "POST", body: body ? JSON.stringify(body) : undefined }) as Run; actionRevision.current += 1; setRun(next); pollerRef.current?.refresh(); if (name === "takeover") window.location.href = `/projects/${params.id}/director`; } catch (reason: unknown) { setError(reason instanceof ApiError ? reason.message : "操作失败"); } finally { setBusy(false); } }
  async function startRun() { setBusy(true); setError(""); try { const next = await api(`/projects/${params.id}/auto-director/runs`, { method: "POST", body: JSON.stringify({ ...start, max_tokens: 0, max_repairs: 2, max_retries: 2, require_direction_confirmation: true, idempotency_key: `auto-ui-${params.id}-${Date.now()}` }) }) as Run; router.replace(directorHref(params.id, next)); setRun(next); setShowStarter(false); } catch (reason: unknown) { setError(reason instanceof ApiError ? reason.message : "无法启动自动导演"); } finally { setBusy(false); } }
  const directions = (run?.context?.directions || []) as Array<Record<string, any>>; const selectedDirection = run?.context?.selected_direction as Record<string, any> | undefined;
  const activeStage = run?.context?.worker_stage && ["FAILED", "BLOCKED"].includes(run.current_stage)
    ? run.context.worker_stage
    : run?.status === "PAUSED" && run.context?.resume_stage
      ? run.context.resume_stage
      : run?.current_stage;
  const currentStep = run?.steps?.find((step) => step.id === run.context?.failed_step_id)
    || run?.steps?.find((step) => step.stage === activeStage);
  const draftStep = latestDraftStep;
  const state = run && recoveryState(run);
  const canOperate = sync.status !== "offline" && !loadError && (!requestedRunId || run?.id === requestedRunId);
  const canPause = canOperate && (state === "RUNNING" || state === "WAITING_RETRY" || state === "QUEUED");
  const canResume = canOperate && state === "PAUSED";
  const canRetry = canOperate && state === "NEEDS_AUTHOR" && run?.current_stage !== "DIRECTION_SELECTION" && run?.pause_reason !== "MAX_RETRIES_REACHED";
  const canTakeover = canOperate && Boolean(run) && state !== "COMPLETED";
  const syncLabel = sync.status === "syncing" ? "同步中" : sync.status === "offline" ? "API 连接暂断" : sync.status === "paused" ? "已暂停同步" : "已同步";
  return <main className="stack"><header className="page-header"><div><p className="eyebrow">AUTO DIRECTOR · LOCAL</p><h1>自动导演</h1><p>从整本方向到指定数量的正文、审核与修复，进度会持续自动刷新。</p></div><div className="row"><span className={`ai-live-sync ai-live-sync-${sync.status}`} role="status" aria-live="polite">{sync.status === "syncing" ? <LoaderCircle size={13} className="ai-live-spin" /> : <CircleDot size={13} />}{syncLabel}{sync.lastUpdatedAt ? ` · ${new Date(sync.lastUpdatedAt).toLocaleTimeString("zh-CN")}` : ""}</span><button className="ai-live-refresh" onClick={() => pollerRef.current?.refresh()} disabled={sync.status === "paused" || sync.status === "syncing"} aria-label="刷新自动导演状态"><RefreshCw size={13} />刷新</button><button className="secondary" onClick={() => setShowStarter((value) => !value)} disabled={busy}>{showStarter ? "返回当前运行" : "新建自动运行"}</button><button className="secondary" onClick={() => action("pause")} disabled={busy || !canPause}><Pause size={15} />暂停</button><button onClick={() => action("resume")} disabled={busy || !canResume}><Play size={15} />继续</button></div></header>
    {error && <section className="section-card run-recovery" role="alert"><CircleAlert size={16} />{error}</section>}
    {loadError && <section className="section-card" role="alert"><p>{loadError}</p><Link href="/">返回书库</Link></section>}
    {sync.status === "offline" && <p className="preview-warning" role="status">与本地服务的连接暂断。{run ? "下方保留上次读取的进度。" : "暂时无法读取运行。"}{sync.retryAt && <> 预计 {recoveryTime(new Date(sync.retryAt).toISOString())} 重新同步。</>}</p>}
    {!loaded && sync.status !== "offline" && <p className="muted" role="status">正在读取运行进度…</p>}
    {(showStarter || (loaded && !requestedRunId && !run)) ? <section className="section-card auto-start-card"><div><p className="eyebrow">START AUTO DIRECTOR</p><h2>让 AI 从这本小说继续创作</h2><p className="muted">系统会自动生成方向、人物、世界边界、整本卷纲、章节任务、正文、审核与可修复问题。你只需随时观看、暂停或接管修改。</p></div><div className="form-grid"><label className="field full"><span>小说灵感 / 已有设定</span><textarea value={start.inspiration} onChange={(event) => setStart({ ...start, inspiration: event.target.value })} placeholder="输入一句灵感，或保留项目中已保存的设定。" /></label><label className="field"><span>整本规划章节数</span><input type="number" min={1} max={100000} value={start.target_chapters} onChange={(event) => setStart({ ...start, target_chapters: Number(event.target.value) })} /></label><label className="field"><span>本次自动生成正文</span><input type="number" min={1} max={100000} value={start.max_chapters} onChange={(event) => setStart({ ...start, max_chapters: Number(event.target.value) })} /></label><label className="field"><span>每章目标字数</span><input type="number" min={500} max={20000} value={start.target_words_per_chapter} onChange={(event) => setStart({ ...start, target_words_per_chapter: Number(event.target.value) })} /></label><label className="field"><span>整体文风</span><input value={start.tone} onChange={(event) => setStart({ ...start, tone: event.target.value })} placeholder="可选，例如克制、悬疑、短句" /></label></div><div className="planning-save-bar"><span>本地运行不设 token 上限；正文数量以“本次自动生成正文”为准。</span><button onClick={() => void startRun()} disabled={busy || !start.inspiration.trim()}><Play size={15} />{busy ? "正在启动…" : "启动自动导演"}</button></div></section> : run && (!requestedRunId || run.id === requestedRunId) && <><section className="section-card"><div className="section-card-header"><div><p className="eyebrow">CURRENT CHECKPOINT</p><h2>{labels[run.current_stage] || run.current_stage}</h2></div></div><RunRecoveryPanel run={run} /><div className="row">{canRetry && <button className="secondary" onClick={() => action("retry")} disabled={busy}><RotateCcw size={15} />重试当前阶段</button>}{canTakeover && <button className="secondary" onClick={() => action("takeover")} disabled={busy}><UserRoundCog size={15} />接管编辑</button>}</div><div className="run-metrics"><span>当前章节：第 {run.context?.current_chapter_number || 1} 章</span><span>已完成：{(run.context?.completed_chapters as unknown[] | undefined)?.length || 0} 章</span><span>自动采用：{run.context?.adopted_chapters || 0} 次</span><span>本次目标：{run.settings?.max_chapters || run.settings?.target_chapters || 0} 章</span><span>整本规划：{run.settings?.target_chapters || 0} 章</span><span>当前阶段 token：{currentStep?.usage_metrics?.total_tokens || currentStep?.token_usage?.total_tokens || currentStep?.token_usage?.tokens || 0}</span><span>累计 token：{run.usage_metrics?.total_tokens || run.token_usage?.total_tokens || 0}</span><span>输入/输出：{run.usage_metrics?.prompt_tokens || 0} / {run.usage_metrics?.completion_tokens || 0}</span><span>调用次数：{run.usage_metrics?.calls || run.token_usage?.total_calls || 0}</span><span>失败/重试：{run.steps.filter((step) => ["FAILED", "BLOCKED"].includes(step.status)).length}</span><span>估算成本：{run.usage_metrics?.cost_status === "UNKNOWN" ? "未知" : (run.usage_metrics?.estimated_cost || "-")}</span></div></section>
      <section className="section-card"><p className="eyebrow">STAGE TIMELINE</p><div className="stage-timeline">{stages.map((stage) => { const done = run.steps.some((step) => step.stage === stage && step.status === "COMMITTED"); const active = stage === activeStage; return <div className={`stage-item ${done ? "done" : ""} ${active ? "active" : ""}`} key={stage}><span>{done ? <Check size={13} /> : active ? "●" : "○"}</span><small>{labels[stage]}</small></div>; })}</div></section>
      {run.current_stage === "DIRECTION_SELECTION" && directions.length > 0 && <section className="section-card"><p className="eyebrow">CHOOSE BOOK DIRECTION</p><h2>确认整本方向后，系统会自动继续</h2><div className="direction-options">{directions.map((direction, index) => <article className="direction-detail" key={`${direction.title}-${index}`}><h3>{direction.title || `方向 ${index + 1}`}</h3><p>{direction.selling_point || direction.premise}</p><p><strong>主角目标：</strong>{direction.protagonist?.desire || "未设置"}</p><p><strong>核心冲突：</strong>{direction.core_conflict || "未设置"}</p><p><strong>前十章承诺：</strong>{(direction.first_ten_chapter_promises || []).join("、") || "未设置"}</p><p><strong>预计篇幅：</strong>{run.settings?.target_chapters || "开放长度"} 章</p><p><strong>文风建议：</strong>{direction.style_advice || "未设置"}</p><button onClick={() => action("select-direction", { direction_index: index })} disabled={busy || !canOperate}><Check size={15} />采用此方向</button></article>)}</div></section>}
      {selectedDirection && <section className="section-card"><div className="section-card-header"><div><p className="eyebrow">SELECTED DIRECTION</p><h2>{selectedDirection.title || "已选方向"}</h2></div><span className="status-badge"><Check size={13} />已确认</span></div><p>{selectedDirection.selling_point || selectedDirection.premise}</p><button className="secondary" type="button" onClick={() => setDirectionOpen((value) => !value)}><Eye size={14} />{directionOpen ? "收起方向详情" : "查看方向详情"}</button>{directionOpen && <div className="direction-detail"><p><strong>主角目标：</strong>{selectedDirection.protagonist?.desire || "未设置"}</p><p><strong>核心冲突：</strong>{selectedDirection.core_conflict || "未设置"}</p><p><strong>前十章承诺：</strong>{(selectedDirection.first_ten_chapter_promises || []).join("、") || "未设置"}</p><p><strong>世界边界：</strong>{(selectedDirection.world_boundaries || []).join("、") || "未设置"}</p><p><strong>文风建议：</strong>{selectedDirection.style_advice || "未设置"}</p></div>}</section>}
      {draftStep && <section className="section-card"><p className="eyebrow">CURRENT CHAPTER</p><h2>第 {displayedChapter} 章</h2><p>草稿引用：{draftStep.output_artifact_id}</p>{task && <div className="run-metrics"><span>章节任务：{task.objective || "已生成"}</span><span>必须事件：{(task.must_events || []).length} 项</span><span>场景节拍：{(task.scene_beats || []).length} 项</span></div>}{quality?.current_assessment && <div className="quality-summary"><strong>质量审核：{quality.current_assessment.status}</strong><span>得分：{quality.current_assessment.overall_score ?? "待定"}</span><small>{(quality.current_assessment.decision_reason_codes || []).join("、") || "未发现阻塞问题"}</small></div>}{(run.context?.quality_debts as string[] | undefined)?.length ? <p className="muted">质量债务：{(run.context?.quality_debts as string[]).join("、")}</p> : null}{draft?.content && <article className="prose-preview">{draft.content}</article>}{run.status === "COMPLETED" && <p className="success-note">整本自动导演已完成。</p>}</section>}
      {chapters.length > 0 && <section className="section-card"><div className="section-card-header"><div><p className="eyebrow">COMPLETED CHAPTERS</p><h2>{run.status === "COMPLETED" ? "本次自动导演已完成" : "已完成章节"}</h2></div><Link className="button secondary" href={`/projects/${params.id}/chapters`}>查看章节列表</Link></div><div className="run-metrics"><span>章节数预估：{run.settings?.target_chapters || "开放长度"}</span><span>已完成章节：{(run.context?.completed_chapters as unknown[] | undefined)?.length || 0}</span><span>最近章节：第 {run.context?.last_adopted_chapter_number || displayedChapter} 章</span></div><div className="completed-chapters">{chapters.filter((chapter) => (run.context?.completed_chapters as unknown[] | undefined)?.includes(chapter.number)).map((chapter) => <Link key={chapter.id} href={`/projects/${params.id}/chapters?chapter=${chapter.id}`}><strong>第 {chapter.number} 章</strong><span>{chapter.title || "未命名章节"}</span></Link>)}</div></section>}
      {currentStep?.error_summary && <section className="section-card"><h2>需要处理</h2><p>{currentStep.error_summary}</p></section>}
    </>}</main>;
}
