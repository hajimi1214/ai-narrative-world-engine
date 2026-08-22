"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowRight, CheckCircle2, FileCheck2, Lock, PenLine, RefreshCw, ShieldCheck, Sparkles } from "lucide-react";
import { api, ApiError } from "../../../lib";
import { EmptyState, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";

type PlanChapter = Record<string, any> & { id: string; number: number; title: string };
type Evaluation = { enabled: boolean; status: string; chapters: Array<Record<string, any>>; summary?: Record<string, any>; continuity?: Record<string, any> };

const display = (value: unknown) => String(value || "未设置");
const list = (value: unknown) => Array.isArray(value) ? value : [];

export default function ChaptersPage({ params }: { params: { id: string } }) {
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [plan, setPlan] = useState<any>(null);
  const [next, setNext] = useState<any>(null);
  const [selectedNumber, setSelectedNumber] = useState<number | null>(null);
  const [task, setTask] = useState<any>(null);
  const [draft, setDraft] = useState<any>(null);
  const [drafts, setDrafts] = useState<any[]>([]);
  const [quality, setQuality] = useState<any>(null);
  const [writerPreview, setWriterPreview] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = async (keepSelection = false) => {
    setBusy(true); setError("");
    try {
      const [evaluationValue, nextValue, planValue] = await Promise.all([
        api(`/projects/${params.id}/long-form/evaluation`) as Promise<Evaluation>,
        api(`/projects/${params.id}/long-form/next`),
        api(`/projects/${params.id}/planning/plan`),
      ]);
      setEvaluation(evaluationValue); setNext(nextValue); setPlan(planValue?.plan === null ? null : planValue);
      const number = keepSelection && selectedNumber ? selectedNumber : nextValue?.task?.number || evaluationValue?.chapters?.[0]?.number || null;
      setSelectedNumber(number);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取章节工作台"); }
    finally { setBusy(false); }
  };

  useEffect(() => { void load(); }, [params.id]);
  const row = useMemo(() => evaluation?.chapters?.find((item) => item.number === selectedNumber) || null, [evaluation, selectedNumber]);
  const planChapter: PlanChapter | null = useMemo(() => plan?.chapters?.find((item: PlanChapter) => item.number === selectedNumber) || null, [plan, selectedNumber]);
  const chapterId = row?.chapter?.id;
  const isLatestDraft = !drafts.length || draft?.id === drafts[0]?.id;

  const loadChapterData = async () => {
    if (!chapterId) { setTask(planChapter); setDraft(null); setDrafts([]); setQuality(null); setWriterPreview(null); return; }
    setBusy(true); setError("");
    try {
      const [taskValue, drafts, qualityValue] = await Promise.all([
        api(`/projects/${params.id}/chapters/${chapterId}/planning-task`),
        api(`/projects/${params.id}/chapters/${chapterId}/writer/drafts`),
        api(`/projects/${params.id}/chapters/${chapterId}/quality`),
      ]);
      setTask(taskValue?.task || planChapter); setQuality(qualityValue); setDrafts(drafts || []);
      const latest = drafts?.[0];
      setDraft(latest ? await api(`/projects/${params.id}/writer-drafts/${latest.id}`) : null);
      setWriterPreview(null);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取章节状态"); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (evaluation) void loadChapterData(); }, [selectedNumber, evaluation]);
  const selectDraft = async (draftId: string) => { setBusy(true); setError(""); try { setDraft(await api(`/projects/${params.id}/writer-drafts/${draftId}`)); } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取草稿版本"); } finally { setBusy(false); } };

  const run = async (path: string, options: RequestInit = {}, success?: string) => {
    if (!chapterId) return;
    setBusy(true); setError(""); setMessage("");
    try { const value = await api(path, options); if (success) setMessage(success); await load(true); await loadChapterData(); return value; }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  };
  const render = () => run(`/projects/${params.id}/chapters/${chapterId}/writer/render`, { method: "POST", body: JSON.stringify({}) }, "正文草稿已生成，请先运行质量检查。");
  const previewWriter = async () => { if (!chapterId) return; setBusy(true); setError(""); try { setWriterPreview(await api(`/projects/${params.id}/chapters/${chapterId}/writer/preview`, { method: "POST", body: JSON.stringify({}) })); } catch (reason) { setError(reason instanceof ApiError ? reason.message : "Writer 预览失败"); } finally { setBusy(false); } };
  const assess = () => run(`/projects/${params.id}/chapters/${chapterId}/quality/assess`, { method: "POST", body: JSON.stringify({}) }, "质量评估完成。");
  const approve = () => quality?.current_assessment?.id && run(`/projects/${params.id}/chapters/${chapterId}/quality/assessments/${quality.current_assessment.id}/approve`, { method: "POST" }, "质量已批准，章节进入正式稿状态。");
  const repair = async () => { const value = quality?.current_assessment?.id && await run(`/projects/${params.id}/chapters/${chapterId}/quality/assessments/${quality.current_assessment.id}/repair`, { method: "POST", body: JSON.stringify({}) }, "修复草稿已生成，请检查后采用。"); if (value?.draft) setDraft(value.draft); };
  const adopt = () => draft?.id && run(`/projects/${params.id}/writer-drafts/${draft.id}/adopt`, { method: "POST", body: JSON.stringify({}) }, "当前草稿已采用。");
  const adoptRepair = () => draft?.id && run(`/projects/${params.id}/chapters/${chapterId}/quality/repairs/${draft.id}/adopt`, { method: "POST" }, "修复稿已采用，请重新运行质量检查。");

  if (!evaluation && !error) return <LoadingState />;
  if (error && !evaluation) return <ErrorState message={error} retry={() => void load()} />;
  if (!evaluation?.enabled) return <main className="stack"><PageHeader title="章节工作台" description="从整本规划开始，逐章完成任务、生成正文并通过质量门禁。" /><SectionCard><EmptyState title="还没有审批规划" description="先填写灵感、故事种子和文风边界，生成并审批整本规划。" action={<Link className="button" href={`/projects/${params.id}/planning`}>进入整本规划 <ArrowRight size={15} /></Link>} /></SectionCard></main>;

  const summary = evaluation.summary || {};
  return <main className="stack chapter-workspace">
    <PageHeader title="章节工作台" description="先完成章节任务单，再生成 Writer 草稿；正文必须通过连续性与反 AI 质量门禁才能采用。" action={<button className="secondary" onClick={() => void load()} disabled={busy}><RefreshCw size={15} />刷新状态</button>} />
    {error && <ErrorState message={error} retry={() => setError("")} />}{message && <div className="success-state"><CheckCircle2 size={16} />{message}</div>}
    <section className="chapter-progress"><div><small>已开始 / 计划</small><strong>{summary.started || 0} / {summary.planned || 0}</strong></div><div><small>质量通过率</small><strong>{Math.round((summary.quality_pass_rate || 0) * 100)}%</strong></div><div><small>任务进度</small><strong>{Math.round((summary.task_progress || 0) * 100)}%</strong></div><div><small>当前状态</small><StatusBadge value={evaluation.status === "HEALTHY" ? "正常" : "需要处理"} /></div><div className="chapter-next"><small>下一章</small><strong>{next?.status === "PLAN_COMPLETE" ? "规划已完成" : next?.task?.number ? `第 ${next.task.number} 章` : "先审批规划"}</strong>{next?.blocked_reasons?.length ? <span><AlertTriangle size={13} />{next.blocked_reasons.join("、")}</span> : null}</div></section>
    <div className="chapter-grid"><section className="chapter-list-panel"><div className="section-heading"><h2>章节任务</h2><span className="muted">{evaluation.chapters.length} 章</span></div>{evaluation.chapters.map((item) => <button type="button" key={item.number} className={`chapter-row ${item.number === selectedNumber ? "active" : ""}`} onClick={() => setSelectedNumber(item.number)}><span>{String(item.number).padStart(2, "0")}</span><div><strong>{item.title || `第 ${item.number} 章`}</strong><small>{item.task_locked ? "任务已锁定" : "任务待锁定"} · {item.quality_status || "未开始"}</small></div><StatusBadge value={item.quality_status || (item.chapter ? item.chapter.status : "未开始")} /></button>)}</section>
      <section className="chapter-editor-panel">{!row ? <div className="chapter-placeholder"><PenLine size={30} /><strong>选择一个章节</strong><p>从左侧选择任务单，查看本章目标和正文状态。</p></div> : <><div className="chapter-editor-header"><div><p className="eyebrow">CHAPTER {String(row.number).padStart(2, "0")}</p><h2>{row.title || `第 ${row.number} 章`}</h2><p>{row.chapter ? `正文状态：${display(row.chapter.status)} · ${row.chapter.word_count || 0} 字` : "尚未形成正式章节正文"}</p></div><StatusBadge value={row.quality_status || (row.task_locked ? "已锁定任务" : "待锁定任务")} /></div>
        {!chapterId ? <div className="chapter-placeholder compact"><Lock size={25} /><strong>{row.task_locked ? "等待正式章节" : "先锁定本章任务单"}</strong><p>{row.task_locked ? "导演或结构投影形成正式 Chapter 后，Writer 操作会在这里开放。" : "回到整本规划，补齐并锁定本章任务单。"}</p><Link className="button secondary" href={row.task_locked ? `/projects/${params.id}/director` : `/projects/${params.id}/planning`}>{row.task_locked ? "去导演台" : "去整本规划"} <ArrowRight size={15} /></Link></div> : <>
          <SectionCard title="本章任务" description="这些边界会同时进入 Writer 和质量门禁。"><div className="task-summary"><div><small>章节目标</small><p>{display(task?.objective)}</p></div><div><small>核心冲突</small><p>{display(task?.conflict)}</p></div><div><small>起始 → 结束状态</small><p>{JSON.stringify(task?.start_state || {})} → {JSON.stringify(task?.end_state || {})}</p></div><div><small>必须事件</small><p>{list(task?.must_events).join("；") || "无"}</p></div><div><small>禁止事件 / 揭示</small><p>{list(task?.forbidden_events).join("；") || "无"} · {list(task?.forbidden_reveals).join("；") || "无"}</p></div></div></SectionCard>
          {writerPreview && <div className="preview-warning"><FileCheck2 size={17} /><span>Writer 已读取 {writerPreview.source_counts?.scenes || 0} 个场景，目标 {writerPreview.target_words || 0} 字；上下文指纹 <code>{writerPreview.writer_context_fingerprint?.slice(0, 12)}</code></span></div>}
          {drafts.length > 0 && <SectionCard title="草稿版本" description="生成新稿不会覆盖旧稿；选择任意已验证版本进行审阅或采用。"><div className="draft-history">{drafts.map((item) => <button type="button" key={item.id} className={`draft-history-row ${draft?.id === item.id ? "active" : ""}`} onClick={() => void selectDraft(item.id)}><span>v{item.version}</span><div><strong>{item.title_candidate || row.title}</strong><small>{item.origin === "QUALITY_REPAIR" ? "质量修复" : "Writer 生成"} · {item.word_count || 0} 字</small></div><StatusBadge value={item.status} /></button>)}</div></SectionCard>}
          {draft ? <article className="prose-preview"><div className="chapter-source-strip"><span><Sparkles size={15} />v{draft.version || "-"} · {draft.status || "草稿"} · {draft.word_count || 0} 字</span><span>{draft.model || "Writer"}</span></div><h3>{draft.chapter_title || row.title}</h3><p>{draft.content || draft.prose || "草稿未包含正文。"}</p></article> : <div className="chapter-placeholder compact"><PenLine size={24} /><strong>尚无正文草稿</strong><p>先预览 Writer 上下文，确认任务边界后再生成。</p></div>}
          {quality?.current_assessment && <SectionCard title="质量门禁" aside={<StatusBadge value={quality.effective_status || quality.current_assessment.status} />}><div className="quality-summary"><div><small>评分</small><strong>{quality.current_assessment.overall_score ?? "-"}</strong></div><div><small>连续性问题</small><strong>{quality.current_assessment.continuity_report?.issues?.length || quality.current_assessment.decision_reason_codes?.length || 0}</strong></div><div><small>反 AI 检查</small><strong>{quality.current_assessment.deterministic_report?.valid === false ? "需修复" : "已运行"}</strong></div></div>{quality.current_assessment.decision_reason_codes?.length ? <p className="muted" style={{ marginTop: 12 }}>原因：{quality.current_assessment.decision_reason_codes.join("、")}</p> : null}</SectionCard>}
          {!isLatestDraft && <div className="preview-warning"><FileCheck2 size={17} /><span>当前正在审阅历史版本。质量检查只针对最新草稿；采用此版本后再重新运行质量门禁。</span></div>}
          <div className="chapter-actions"><button className="secondary" onClick={previewWriter} disabled={busy}><FileCheck2 size={15} />预览 Writer 上下文</button><button onClick={render} disabled={busy}><Sparkles size={15} />生成正文草稿</button><button className="secondary" onClick={assess} disabled={busy || !draft || !isLatestDraft}><ShieldCheck size={15} />运行质量检查</button>{quality?.effective_status === "PASS" && quality.current_assessment?.id ? <button onClick={approve} disabled={busy}><CheckCircle2 size={15} />批准质量</button> : null}{quality?.repair_available && <button className="secondary" onClick={repair} disabled={busy}><RefreshCw size={15} />生成修复稿</button>}{draft?.status === "VALIDATED" && draft?.origin !== "QUALITY_REPAIR" && <button className="secondary" onClick={adopt} disabled={busy}><CheckCircle2 size={15} />采用当前稿</button>}{draft?.origin === "QUALITY_REPAIR" && draft?.status === "VALIDATED" && <button onClick={adoptRepair} disabled={busy}><CheckCircle2 size={15} />采用修复稿</button>}</div>
        </>}</>}</section></div>
    {evaluation.continuity?.timeline_errors?.length ? <div className="preview-warning"><AlertTriangle size={17} /><span>正式历史时间线存在 {evaluation.continuity.timeline_errors.length} 个错误，建议先处理连续性问题再继续生成。</span></div> : null}
  </main>;
}
