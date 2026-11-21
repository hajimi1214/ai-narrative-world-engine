"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, ArrowRight, CheckCircle2, Download, FileCheck2, Lock, MapPin, PenLine, RefreshCw, RotateCcw, Save, ShieldCheck, Sparkles, X } from "lucide-react";
import { api, apiUrl, ApiError } from "../../../lib";
import { EmptyState, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";
import styles from "./authoring.module.css";
import { compareDraftLines } from "../../../draft-diff";
import { mapFindingSpan } from "../../../finding-span";
import { assessEventEvidence } from "../../../event-evidence";

type PlanChapter = Record<string, any> & { id: string; number: number; title: string };
type Evaluation = { enabled: boolean; status: string; chapters: Array<Record<string, any>>; summary?: Record<string, any>; continuity?: Record<string, any> };

const display = (value: unknown) => String(value || "未设置");
const list = (value: unknown) => Array.isArray(value) ? value : [];

export default function ChaptersPage({ params }: { params: { id: string } }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [plan, setPlan] = useState<any>(null);
  const [next, setNext] = useState<any>(null);
  const [selectedNumber, setSelectedNumber] = useState<number | null>(null);
  const [task, setTask] = useState<any>(null);
  const [draft, setDraft] = useState<any>(null);
  const [drafts, setDrafts] = useState<any[]>([]);
  const [quality, setQuality] = useState<any>(null);
  const [qualityFindings, setQualityFindings] = useState<any[]>([]);
  const [writerPreview, setWriterPreview] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [editBase, setEditBase] = useState<{ draftId: string; currentId: string | null; key: string } | null>(null);
  const [restoringVersion, setRestoringVersion] = useState(false);
  const [selectedFinding, setSelectedFinding] = useState<any>(null);
  const [compareCurrent, setCompareCurrent] = useState<any>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [volumes, setVolumes] = useState<Array<{ id: string; title: string; volume_number: number }>>([]);
  const [exportVolume, setExportVolume] = useState("");
  const [exportFormat, setExportFormat] = useState("txt");
  const [approvedOnly, setApprovedOnly] = useState(false);
  const chapterLoad = useRef(0);
  const draftLoad = useRef(0);
  const workspaceLoad = useRef(0);
  const actionGeneration = useRef(0);
  const chapterRequest = useRef<AbortController | null>(null);
  const currentChapterId = useRef<string | undefined>(undefined);
  const hasUnsavedChanges = editing && editContent !== (draft?.content || draft?.prose || "");
  const leaveEditor = () => !hasUnsavedChanges || window.confirm("当前修改尚未保存，确定放弃吗？");
  useEffect(() => {
    const protect = (event: BeforeUnloadEvent) => { if (hasUnsavedChanges) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [hasUnsavedChanges]);

  const load = async (keepSelection = false) => {
    const generation = ++workspaceLoad.current;
    const controller = new AbortController();
    setBusy(true); setError("");
    try {
      const [evaluationValue, nextValue, planValue, volumeValue] = await Promise.all([
        api(`/projects/${params.id}/long-form/evaluation`, { signal: controller.signal }) as Promise<Evaluation>,
        api(`/projects/${params.id}/long-form/next`, { signal: controller.signal }),
        api(`/projects/${params.id}/planning/plan`, { signal: controller.signal }),
        api(`/projects/${params.id}/volumes`, { signal: controller.signal }),
      ]);
      if (generation !== workspaceLoad.current || controller.signal.aborted) return;
      setVolumes(Array.isArray(volumeValue) ? volumeValue : []);
      setEvaluation(evaluationValue); setNext(nextValue); setPlan(planValue?.plan === null ? null : planValue);
      const requestedId = searchParams.get("chapter");
      const requestedNumber = Number(searchParams.get("chapter_number"));
      const requested = requestedId ? evaluationValue.chapters.find((item) => item.chapter?.id === requestedId || item.id === requestedId) : evaluationValue.chapters.find((item) => item.number === requestedNumber);
      const number = requested?.number || (keepSelection && selectedNumber ? selectedNumber : nextValue?.task?.number || evaluationValue?.chapters?.[0]?.number || null);
      setSelectedNumber(number);
      return evaluationValue;
    } catch (reason) { if (generation === workspaceLoad.current && !controller.signal.aborted) setError(reason instanceof ApiError ? reason.message : "无法读取章节工作台"); }
    finally { if (generation === workspaceLoad.current) setBusy(false); }
  };

  useEffect(() => { void load(); return () => { workspaceLoad.current += 1; }; }, [params.id]);
  const row = useMemo(() => evaluation?.chapters?.find((item) => item.number === selectedNumber) || null, [evaluation, selectedNumber]);
  const planChapter: PlanChapter | null = useMemo(() => plan?.chapters?.find((item: PlanChapter) => item.number === selectedNumber) || null, [plan, selectedNumber]);
  const chapterId = row?.chapter?.id;
  currentChapterId.current = chapterId;
  const isCurrentDraft = Boolean(draft?.id && draft.id === row?.chapter?.current_writer_draft_id);
  // Findings are only actionable when the assessment and the loaded draft share
  // both identity and content fingerprint. This closes the small cross-tab race
  // between loading quality and loading the current draft.
  const findingsMatchDraft = Boolean(
    quality?.current_assessment?.writer_draft_id &&
    quality.current_assessment.writer_draft_id === draft?.id &&
    quality.current_assessment.content_fingerprint &&
    quality.current_assessment.content_fingerprint === draft?.content_fingerprint,
  );
  const eventReviews = list(quality?.current_assessment?.critic_report?.mandatory_event_reviews);
  const requiredEventCount = list(task?.must_events).length + list(task?.foreshadow_payoff).length;

  const loadChapterData = async (selectedRow = row) => {
    const selectedChapterId = selectedRow?.chapter?.id;
    const loadId = ++chapterLoad.current;
    chapterRequest.current?.abort();
    const controller = new AbortController();
    chapterRequest.current = controller;
    ++draftLoad.current;
    setTask(selectedRow ? plan?.chapters?.find((item: PlanChapter) => item.number === selectedRow.number) || null : null);
    setDraft(null); setDrafts([]); setQuality(null); setQualityFindings([]); setSelectedFinding(null); setWriterPreview(null); setError("");
    if (!selectedChapterId) { setEditing(false); setEditBase(null); setBusy(false); return; }
    setBusy(true); setError("");
    try {
      const [taskValue, drafts, qualityValue] = await Promise.all([
        api(`/projects/${params.id}/chapters/${selectedChapterId}/planning-task`, { signal: controller.signal }),
        api(`/projects/${params.id}/chapters/${selectedChapterId}/writer/drafts`, { signal: controller.signal }),
        api(`/projects/${params.id}/chapters/${selectedChapterId}/quality`, { signal: controller.signal }),
      ]);
      if (loadId !== chapterLoad.current || controller.signal.aborted) return;
      setTask(taskValue?.task || planChapter); setQuality(qualityValue); setDrafts(drafts || []);
      const assessmentId = qualityValue?.current_assessment?.id;
      const assessmentDetail = assessmentId ? await api(`/projects/${params.id}/chapters/${selectedChapterId}/quality/assessments/${assessmentId}`, { signal: controller.signal }).catch(() => null) : null;
      if (loadId !== chapterLoad.current || controller.signal.aborted) return;
      setQualityFindings(assessmentDetail?.findings || []);
      const latest = drafts?.find((item: any) => item.id === selectedRow?.chapter?.current_writer_draft_id) || drafts?.find((item: any) => !["FAILED", "REJECTED", "GENERATING", "STALE"].includes(item.status)) || drafts?.[0];
      const fullDraft = latest ? await api(`/projects/${params.id}/writer-drafts/${latest.id}`, { signal: controller.signal }) : null;
      if (loadId !== chapterLoad.current || controller.signal.aborted) return;
      setDraft(fullDraft);
      setWriterPreview(null);
    } catch (reason) { if (loadId === chapterLoad.current && !controller.signal.aborted) setError(reason instanceof ApiError ? reason.message : "无法读取章节状态"); }
    finally { if (loadId === chapterLoad.current) setBusy(false); }
  };
  useEffect(() => { if (!evaluation) return; void loadChapterData(); return () => chapterRequest.current?.abort(); }, [selectedNumber, evaluation]);
  const selectDraft = async (draftId: string) => {
    if (!leaveEditor()) return;
    const loadId = ++draftLoad.current;
    setEditing(false); setBusy(true); setError("");
    setCompareCurrent(null); setShowDiff(false);
    try {
      const value = await api(`/projects/${params.id}/writer-drafts/${draftId}`);
      if (loadId !== draftLoad.current) return;
      setDraft(value);
      const currentId = row?.chapter?.current_writer_draft_id;
      if (currentId && currentId !== draftId) {
        const current = await api(`/projects/${params.id}/writer-drafts/${currentId}`);
        if (loadId === draftLoad.current) setCompareCurrent(current);
      }
    }
    catch (reason) { if (loadId === draftLoad.current) setError(reason instanceof ApiError ? reason.message : "无法读取草稿版本"); }
    finally { if (loadId === draftLoad.current) setBusy(false); }
  };
  const selectChapter = (number: number) => { if (!leaveEditor()) return; ++draftLoad.current; ++actionGeneration.current; chapterRequest.current?.abort(); setEditing(false); setEditBase(null); setRestoringVersion(false); setCompareCurrent(null); setShowDiff(false); setSelectedFinding(null); setDraft(null); setDrafts([]); setQuality(null); setQualityFindings([]); setWriterPreview(null); setTask(null); setSelectedNumber(number); router.replace(`/projects/${params.id}/chapters?chapter_number=${number}`, { scroll: false }); };

  const run = async (path: string, options: RequestInit = {}, success?: string) => {
    if (!chapterId) return;
    const actionId = ++actionGeneration.current;
    const actionChapterId = chapterId;
    setBusy(true); setError(""); setMessage("");
    try {
      const value = await api(path, options);
      if (actionId !== actionGeneration.current || currentChapterId.current !== actionChapterId) return value;
      if (success) setMessage(success);
      const fresh = await load(true);
      if (actionId !== actionGeneration.current || currentChapterId.current !== actionChapterId) return value;
      await loadChapterData(fresh?.chapters.find((item) => item.chapter?.id === actionChapterId));
      return value;
    } catch (reason) { if (actionId === actionGeneration.current && currentChapterId.current === actionChapterId) setError(reason instanceof ApiError ? reason.message : "操作失败"); }
    finally { if (actionId === actionGeneration.current) setBusy(false); }
  };
  const render = () => run(`/projects/${params.id}/chapters/${chapterId}/writer/render`, { method: "POST", body: JSON.stringify({}) }, "正文草稿已生成，请先运行质量检查。");
  const previewWriter = async () => {
    if (!chapterId) return;
    const actionId = ++actionGeneration.current;
    const previewChapterId = chapterId;
    setBusy(true); setError("");
    try {
      const value = await api(`/projects/${params.id}/chapters/${previewChapterId}/writer/preview`, { method: "POST", body: JSON.stringify({}) });
      if (actionId === actionGeneration.current && currentChapterId.current === previewChapterId) setWriterPreview(value);
    } catch (reason) { if (actionId === actionGeneration.current && currentChapterId.current === previewChapterId) setError(reason instanceof ApiError ? reason.message : "Writer 预览失败"); }
    finally { if (actionId === actionGeneration.current) setBusy(false); }
  };
  const assess = () => run(`/projects/${params.id}/chapters/${chapterId}/quality/assess`, { method: "POST", body: JSON.stringify({}) }, "质量评估完成。");
  const approve = () => quality?.current_assessment?.id && run(`/projects/${params.id}/chapters/${chapterId}/quality/assessments/${quality.current_assessment.id}/approve`, { method: "POST" }, "质量已批准，章节进入正式稿状态。");
  const repair = async () => { const value = quality?.current_assessment?.id && await run(`/projects/${params.id}/chapters/${chapterId}/quality/assessments/${quality.current_assessment.id}/repair`, { method: "POST", body: JSON.stringify({}) }, "修复草稿已生成，请检查后采用。"); if (value?.draft) setDraft(value.draft); };
  const adopt = () => draft?.id && run(`/projects/${params.id}/writer-drafts/${draft.id}/adopt`, { method: "POST", body: JSON.stringify({}) }, "当前草稿已采用。");
  const adoptRepair = () => draft?.id && run(`/projects/${params.id}/chapters/${chapterId}/quality/repairs/${draft.id}/adopt`, { method: "POST" }, "修复稿已采用，请重新运行质量检查。");
  const beginEdit = () => { setError(""); setMessage(""); setRestoringVersion(false); setEditContent(draft?.content || draft?.prose || ""); setEditBase({ draftId: draft.id, currentId: row?.chapter?.current_writer_draft_id || null, key: `author-${crypto.randomUUID()}` }); setEditing(true); };
  const restoreVersion = () => {
    if (!draft?.id || !row?.chapter?.current_writer_draft_id || draft.id === row.chapter.current_writer_draft_id || !leaveEditor()) return;
    setError(""); setMessage(""); setShowDiff(false); setRestoringVersion(true);
    setEditContent(draft.content || draft.prose || "");
    setEditBase({ draftId: draft.id, currentId: row.chapter.current_writer_draft_id, key: `restore-${crypto.randomUUID()}` });
    setEditing(true);
  };
  const locateFinding = (finding: any) => {
    if (!draft?.content || !isCurrentDraft || !findingsMatchDraft) return;
    const span = mapFindingSpan(draft.content, finding.start_offset, finding.end_offset, finding.excerpt);
    if (!span) return;
    if (!leaveEditor()) return;
    setEditing(true); setRestoringVersion(false); setSelectedFinding({ id: finding.id, excerpt: finding.excerpt, ...span });
    setEditContent(draft.content);
    setEditBase({ draftId: draft.id, currentId: row?.chapter?.current_writer_draft_id || null, key: `author-finding-${crypto.randomUUID()}` });
    window.setTimeout(() => {
      const textarea = document.getElementById("chapter-prose") as HTMLTextAreaElement | null;
      if (!textarea) return;
      const lineNumber = draft.content.slice(0, span.start).split("\n").length - 1;
      const lineHeight = Number.parseFloat(window.getComputedStyle(textarea).lineHeight) || 30;
      textarea.focus(); textarea.scrollTop = Math.max(0, lineNumber * lineHeight - textarea.clientHeight / 2); textarea.setSelectionRange(span.start, span.end);
      textarea.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 0);
  };
  const saveEdit = async () => {
    if (!editBase || !editContent.trim()) { setError("正文不能为空。"); return; }
    const restored = restoringVersion;
    const value = await run(`/projects/${params.id}/chapters/${chapterId}/writer/edit`, { method: "POST", body: JSON.stringify({ content: editContent, base_draft_id: editBase.draftId, expected_current_draft_id: editBase.currentId, client_request_id: editBase.key }) }, restored ? "历史内容已恢复为一个新的工作稿版本；原版本保留，请重新审核。" : "修改已保存为新的工作稿，旧版本已保留。请重新检查并批准质量。");
    if (value) { setDraft(value); setEditing(false); setEditBase(null); setRestoringVersion(false); setCompareCurrent(null); setSelectedFinding(null); }
  };
  const downloadManuscript = async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      const query = new URLSearchParams({ format: exportFormat, approved_only: String(approvedOnly) });
      if (exportVolume) query.set("volume_id", exportVolume);
      const response = await fetch(`${apiUrl}/projects/${params.id}/manuscript?${query}`);
      if (!response.ok) { const detail = await response.json().catch(() => null); throw new Error(detail?.detail?.message || "无法导出正文，请确认所选范围已有保存的正文。"); }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `小说-${exportVolume ? "单卷" : "全书"}-${approvedOnly ? "批准稿" : "工作稿"}.${exportFormat}`; document.body.appendChild(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMessage("成稿已下载。导出包含已保存正文，未保存的修改不会进入文件。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "导出失败"); }
    finally { setBusy(false); }
  };

  if (!evaluation && !error) return <LoadingState />;
  if (error && !evaluation) return <ErrorState message={error} retry={() => void load()} />;
  if (!evaluation?.enabled) return <main className="stack"><PageHeader title="章节工作台" description="从整本规划开始，逐章完成任务、生成正文并通过质量门禁。" /><SectionCard><EmptyState title="还没有审批规划" description="先填写灵感、故事种子和文风边界，生成并审批整本规划。" action={<Link className="button" href={`/projects/${params.id}/planning`}>进入整本规划 <ArrowRight size={15} /></Link>} /></SectionCard></main>;

  const summary = evaluation.summary || {};
  return <main className="stack chapter-workspace">
    <PageHeader title="章节工作台" description="逐章生成与改稿，保存工作稿后重新审核，质量批准后进入正式稿。" action={<button className="secondary" onClick={() => { if (leaveEditor()) { setEditing(false); void load(true); } }} disabled={busy}><RefreshCw size={15} />刷新状态</button>} />
    <div className={styles.toolbar} aria-label="成稿导出"><label>导出范围<select aria-label="导出范围" value={exportVolume} onChange={(event) => setExportVolume(event.target.value)}><option value="">整本小说</option>{volumes.map((volume) => <option key={volume.id} value={volume.id}>{volume.title || `第 ${volume.volume_number} 卷`}</option>)}</select></label><label>格式<select aria-label="成稿格式" value={exportFormat} onChange={(event) => setExportFormat(event.target.value)}><option value="txt">TXT 文本</option><option value="md">Markdown</option></select></label><label><input type="checkbox" checked={approvedOnly} onChange={(event) => setApprovedOnly(event.target.checked)} />仅已批准稿</label><button className="secondary" onClick={() => void downloadManuscript()} disabled={busy}><Download size={15} />下载成稿</button></div>
    {error && <ErrorState message={error} retry={() => setError("")} />}{message && <div className="success-state"><CheckCircle2 size={16} />{message}</div>}
    <section className="chapter-progress"><div><small>已开始 / 计划</small><strong>{summary.started || 0} / {summary.planned || 0}</strong></div><div><small>质量通过率</small><strong>{Math.round((summary.quality_pass_rate || 0) * 100)}%</strong></div><div><small>任务进度</small><strong>{Math.round((summary.task_progress || 0) * 100)}%</strong></div><div><small>当前状态</small><StatusBadge value={evaluation.status === "HEALTHY" ? "正常" : "需要处理"} /></div><div className="chapter-next"><small>下一章</small><strong>{next?.status === "PLAN_COMPLETE" ? "规划已完成" : next?.task?.number ? `第 ${next.task.number} 章` : "先审批规划"}</strong>{next?.blocked_reasons?.length ? <span><AlertTriangle size={13} />{next.blocked_reasons.join("、")}</span> : null}</div></section>
    <div className="chapter-grid"><section className="chapter-list-panel"><div className="section-heading"><h2>章节任务</h2><span className="muted">{evaluation.chapters.length} 章</span></div>{evaluation.chapters.map((item) => <button type="button" key={item.number} className={`chapter-row ${item.number === selectedNumber ? "active" : ""}`} onClick={() => selectChapter(item.number)}><span>{String(item.number).padStart(2, "0")}</span><div><strong>{item.title || `第 ${item.number} 章`}</strong><small>{item.task_locked ? "任务已锁定" : "任务待锁定"} · {item.quality_status || "未开始"}</small></div><StatusBadge value={item.quality_status || (item.chapter ? item.chapter.status : "未开始")} /></button>)}</section>
      <section className="chapter-editor-panel">{!row ? <div className="chapter-placeholder"><PenLine size={30} /><strong>选择一个章节</strong><p>从左侧选择任务单，查看本章目标和正文状态。</p></div> : <><div className="chapter-editor-header"><div><p className="eyebrow">CHAPTER {String(row.number).padStart(2, "0")}</p><h2>{row.title || `第 ${row.number} 章`}</h2><p>{row.chapter ? `正文状态：${display(row.chapter.status)} · ${row.chapter.word_count || 0} 字` : "尚未形成正式章节正文"}</p></div><StatusBadge value={row.quality_status || (row.task_locked ? "已锁定任务" : "待锁定任务")} /></div>
        {!chapterId ? <div className="chapter-placeholder compact"><Lock size={25} /><strong>{row.task_locked ? "等待正式章节" : "先锁定本章任务单"}</strong><p>{row.task_locked ? "导演或结构投影形成正式 Chapter 后，Writer 操作会在这里开放。" : "回到整本规划，补齐并锁定本章任务单。"}</p><Link className="button secondary" href={row.task_locked ? `/projects/${params.id}/director` : `/projects/${params.id}/planning`}>{row.task_locked ? "去导演台" : "去整本规划"} <ArrowRight size={15} /></Link></div> : <>
          <SectionCard title="本章任务" description="这些边界会同时进入 Writer 和质量门禁。"><div className="task-summary"><div><small>章节目标</small><p>{display(task?.objective)}</p></div><div><small>核心冲突</small><p>{display(task?.conflict)}</p></div><div><small>起始 → 结束状态</small><p>{JSON.stringify(task?.start_state || {})} → {JSON.stringify(task?.end_state || {})}</p></div><div><small>必须事件</small><p>{list(task?.must_events).join("；") || "无"}</p></div><div><small>禁止事件 / 揭示</small><p>{list(task?.forbidden_events).join("；") || "无"} · {list(task?.forbidden_reveals).join("；") || "无"}</p></div></div></SectionCard>
          {qualityFindings.length > 0 && <SectionCard title="逐条审阅意见" description="意见附有可验证的正文原句时，可直接跳到对应位置编辑。"><div className={styles.findingsList}>{qualityFindings.map((finding) => {
            const span = mapFindingSpan(draft?.content, finding.start_offset, finding.end_offset, finding.excerpt);
            const severity = String(finding.severity || "INFO");
            return <article key={finding.id} className={styles.findingCard}><div className={styles.findingTitle}><StatusBadge value={severity === "BLOCKING" ? "必须修改" : severity === "MAJOR" ? "建议修复" : severity === "MINOR" ? "小建议" : "参考"} /><strong>{finding.message}</strong></div>{finding.excerpt ? <blockquote className={styles.findingExcerpt}>{finding.excerpt}</blockquote> : null}<div className={styles.findingFooter}><small>{finding.source === "DETERMINISTIC" ? "规则检查" : "模型审核"} · {finding.rule_code}</small>{span && isCurrentDraft && findingsMatchDraft ? <button className="secondary" onClick={() => locateFinding(finding)} disabled={busy}><MapPin size={13} />跳到正文</button> : span && !findingsMatchDraft ? <small>质量意见对应的稿件版本已变化，请刷新并重新运行质量检查。</small> : span ? <small>当前正在查看历史版本；请先回到工作稿再定位。</small> : !draft && quality?.current_assessment?.writer_draft_id ? <small>正在读取正文并核对审阅版本。</small> : <small>没有可验证的正文原句，需根据意见手动查看。</small>}</div></article>;
          })}</div></SectionCard>}
          {(requiredEventCount > 0 || eventReviews.length > 0) && <SectionCard title="必需剧情事件证据" description="每条证据对应本次审核稿的原文范围；未确认、缺少引用或引用不匹配正文的事件不会作为已完成。"><div className={styles.eventEvidenceList}>
            {!findingsMatchDraft && <div className={styles.eventEvidenceNotice} role="status">当前打开的草稿与审核记录不匹配。请切换到审核对应版本，核对事件原文。</div>}
            {findingsMatchDraft && requiredEventCount > eventReviews.length && <div className={styles.eventEvidenceNotice} role="status">审核记录缺少 {requiredEventCount - eventReviews.length} 条必需事件确认。</div>}
            {findingsMatchDraft && requiredEventCount > 0 && eventReviews.length === 0 && <div className={styles.eventEvidenceNotice} role="status">此审核记录没有逐条剧情事件证据。</div>}
            {eventReviews.map((review: any, index: number) => {
              const evidence = assessEventEvidence(draft?.content || draft?.prose, review, findingsMatchDraft);
              const statusText = evidence.status === "CONFIRMED" ? "审核确认" : evidence.status === "NOT_CONFIRMED" ? "未确认" : evidence.status === "INVALID_EVIDENCE" ? "引用不匹配正文" : "稿件版本不匹配";
              const statusClass = evidence.status === "CONFIRMED" ? styles.eventEvidenceConfirmed : styles.eventEvidenceBlocked;
              const reference = String(review.requirement_ref || `${review.requirement_type || "event"}:${review.event_ref || index}`);
              const eventType = review.requirement_type === "foreshadow_payoff" ? "伏笔回收" : "必需事件";
              return <article className={styles.eventEvidenceItem} key={reference}>
                <div className={styles.eventEvidenceHeader}><span className={`${styles.eventEvidenceStatus} ${statusClass}`}>{statusText}</span><span className={styles.eventEvidenceKind}>{eventType}</span></div>
                <strong>{review.label || review.event_ref || "未命名事件"}</strong>
                {evidence.span && review.excerpt ? <blockquote className={styles.eventEvidenceQuote}>{review.excerpt}</blockquote> : null}
                {review.reason ? <p className={styles.eventEvidenceReason}>{review.reason}</p> : null}
                <div className={styles.eventEvidenceFooter}>
                  <small>{review.event_ref || reference}</small>
                  {evidence.span && isCurrentDraft ? <button className="secondary" onClick={() => locateFinding({ id: reference, start_offset: review.start_offset, end_offset: review.end_offset, excerpt: review.excerpt })} disabled={busy}><MapPin size={13} />跳到正文</button> : evidence.span && !isCurrentDraft ? <small>证据对应历史稿；恢复为工作稿后可定位编辑。</small> : evidence.status === "STALE" ? <small>切换到审核对应的稿件版本以查看原句。</small> : null}
                </div>
              </article>;
            })}
            {requiredEventCount === 0 && eventReviews.length === 0 && <p className="muted">本章没有待确认的必需事件。</p>}
          </div></SectionCard>}
          {writerPreview && <div className="preview-warning"><FileCheck2 size={17} /><span>Writer 已读取 {writerPreview.source_counts?.scenes || 0} 个场景，目标 {writerPreview.target_words || 0} 字；上下文指纹 <code>{writerPreview.writer_context_fingerprint?.slice(0, 12)}</code></span></div>}
          {drafts.length > 0 && <SectionCard title="草稿版本" description="历史版本可以比较，并恢复为新的工作稿；原稿和审核记录不会被覆盖。"><div className="draft-history">{drafts.map((item) => <button type="button" key={item.id} disabled={busy} className={`draft-history-row ${draft?.id === item.id ? "active" : ""}`} onClick={() => void selectDraft(item.id)}><span>v{item.version}</span><div><strong>{item.title_candidate || row.title}</strong><small>{item.origin === "AUTHOR_EDIT" ? "作者改稿" : item.origin === "QUALITY_REPAIR" ? "质量修复" : "Writer 生成"} · {item.word_count || 0} 字</small></div><StatusBadge value={item.status} /></button>)}</div></SectionCard>}
          {draft && !editing && !isCurrentDraft && !["FAILED", "REJECTED", "GENERATING", "STALE"].includes(draft.status) && row?.chapter?.current_writer_draft_id && <div className={styles.historyActions}><button className="secondary" onClick={() => setShowDiff((value) => !value)} disabled={busy}>{showDiff ? "关闭版本对比" : "比较当前工作稿"}</button><button onClick={restoreVersion} disabled={busy}><RotateCcw size={15} />恢复此版本为新稿</button></div>}
          {showDiff && draft && compareCurrent && <SectionCard title={`v${draft.version} 与当前工作稿对比`} description="红色为选中历史版本内容，绿色为当前工作稿新增内容。"><div className={styles.diffLegend}><span>历史版本 v{draft.version}</span><ArrowRight size={14}/><span>当前工作稿 v{compareCurrent.version}</span></div><div className={styles.diffLines}>{compareDraftLines(draft.content || "", compareCurrent.content || "").map((line, index) => <pre key={`${line.kind}-${index}`} className={`${styles.diffLine} ${styles[`diff_${line.kind}`]}`}>{line.text || " "}</pre>)}</div></SectionCard>}
          {editing ? <div className={styles.editor}><label htmlFor="chapter-prose">{restoringVersion ? `恢复第 ${row.number} 章旧版本` : `编辑第 ${row.number} 章正文`}</label><p className={styles.hint}>{selectedFinding ? "已跳到这条意见所引用的原文位置；选中片段可以直接修改。" : restoringVersion ? "恢复操作会把历史内容另存为新的工作稿。保存后需重新审核，原稿与全部历史保留。" : "保存为新的工作稿版本，原文保留在版本记录中。修改后需要重新审核；自动创作运行时，请先到导演台暂停。"}</p><textarea id="chapter-prose" value={editContent} onChange={(event) => setEditContent(event.target.value)} disabled={busy} spellCheck={false} /><div className={styles.editorActions}><button className="secondary" onClick={() => { if (leaveEditor()) { setEditing(false); setRestoringVersion(false); setSelectedFinding(null); } }} disabled={busy}><X size={15} />取消修改</button><button onClick={() => void saveEdit()} disabled={busy || !editContent.trim() || (!hasUnsavedChanges && !restoringVersion)}><Save size={15} />{restoringVersion ? "另存恢复版本" : "保存工作稿"}</button></div></div> : draft ? <article className="prose-preview"><div className="chapter-source-strip"><span><Sparkles size={15} />v{draft.version || "-"} · {draft.status || "草稿"} · {draft.word_count || 0} 字</span><span>{draft.origin === "AUTHOR_EDIT" ? "作者改稿" : draft.model || "Writer"}</span></div><h3>{draft.chapter_title || row.title}</h3><p>{draft.content || draft.prose || "草稿未包含正文。"}</p></article> : <div className="chapter-placeholder compact"><PenLine size={24} /><strong>尚无正文草稿</strong><p>先预览 Writer 上下文，确认任务边界后再生成。</p></div>}
          {quality?.current_assessment && <SectionCard title="质量门禁" aside={<StatusBadge value={quality.effective_status || quality.current_assessment.status} />}><div className="quality-summary"><div><small>评分</small><strong>{quality.current_assessment.overall_score ?? "-"}</strong></div><div><small>连续性问题</small><strong>{quality.current_assessment.continuity_report?.issues?.length || quality.current_assessment.decision_reason_codes?.length || 0}</strong></div><div><small>反 AI 检查</small><strong>{quality.current_assessment.deterministic_report?.valid === false ? "需修复" : "已运行"}</strong></div></div>{quality.current_assessment.decision_reason_codes?.length ? <p className="muted" style={{ marginTop: 12 }}>原因：{quality.current_assessment.decision_reason_codes.join("、")}</p> : null}</SectionCard>}
          {!isCurrentDraft && draft && <div className="preview-warning"><FileCheck2 size={17} /><span>此版本尚未作为当前工作稿。质量检查针对当前工作稿；可将此版本编辑保存为新稿。</span></div>}
          {!editing && <div className={`chapter-actions ${styles.actions}`}><button className="secondary" onClick={previewWriter} disabled={busy}><FileCheck2 size={15} />预览 Writer 上下文</button><button onClick={render} disabled={busy}><Sparkles size={15} />生成正文草稿</button>{draft?.content && !["FAILED", "REJECTED", "GENERATING", "STALE"].includes(draft.status) && <button className="secondary" onClick={beginEdit} disabled={busy}><PenLine size={15} />编辑正文</button>}<button className="secondary" onClick={assess} disabled={busy || !isCurrentDraft}><ShieldCheck size={15} />运行质量检查</button>{quality?.effective_status === "PASS" && quality.current_assessment?.id && isCurrentDraft ? <button onClick={approve} disabled={busy}><CheckCircle2 size={15} />批准为正式稿</button> : null}{quality?.repair_available && isCurrentDraft && <button className="secondary" onClick={repair} disabled={busy}><RefreshCw size={15} />生成修复稿</button>}{draft?.status === "VALIDATED" && draft?.origin !== "QUALITY_REPAIR" && <button className="secondary" onClick={adopt} disabled={busy}><CheckCircle2 size={15} />设为工作稿</button>}{draft?.origin === "QUALITY_REPAIR" && draft?.status === "VALIDATED" && <button onClick={adoptRepair} disabled={busy}><CheckCircle2 size={15} />采用修复稿</button>}</div>}
        </>}</>}</section></div>
    {evaluation.continuity?.timeline_errors?.length ? <div className="preview-warning"><AlertTriangle size={17} /><span>正式历史时间线存在 {evaluation.continuity.timeline_errors.length} 个错误，建议先处理连续性问题再继续生成。</span></div> : null}
  </main>;
}
