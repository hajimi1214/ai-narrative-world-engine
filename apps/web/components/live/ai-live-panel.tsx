"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, CircleDot, Clock3, FileText, Layers3, LoaderCircle, Radio, X } from "lucide-react";
import { api } from "../../app/lib";

type Trace = { id: string; stage?: string; status?: string; model?: string | null; latency_ms?: number | null; error_code?: string | null; created_at?: string | null; source_type?: string | null; validation_report?: { live_phase?: string; live_message?: string; generation_warning?: string } };
type AutoRun = { id: string; status?: string; current_stage?: string; updated_at?: string; pause_reason?: string; steps?: Array<{ id: string; stage?: string; status?: string; error_code?: string; started_at?: string; completed_at?: string; usage_metrics?: { model?: string; latency_ms?: number } }> };

const stageLabels: Record<string, string> = { WRITER: "正文写作", CRITIC: "章节审核", REPAIR: "问题修复", DIRECTOR: "导演规划", CHARACTER_ACTOR: "角色行动", WORLD_RESOLVER: "世界裁定", SCENE_COMMIT: "保存场景", AUTONOMOUS_LOOP: "自动推进" };
const statusLabels: Record<string, string> = { STARTED: "进行中", SUCCEEDED: "已完成", FAILED: "失败", BLOCKED: "已暂停" };
const stageText = (value?: string, sourceType?: string | null) => value === "DIRECTOR" && sourceType === "STORY_PLAN" ? "整书规划" : stageLabels[value || ""] || value || "AI 任务";
const statusText = (value?: string) => statusLabels[value || ""] || value || "等待中";
const timeText = (value?: string | null) => value ? new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "刚刚";
const elapsedText = (trace: Trace) => trace.latency_ms ? `${Math.max(1, Math.round(trace.latency_ms / 1000))} 秒` : trace.status === "STARTED" && trace.created_at ? `${Math.max(1, Math.floor((Date.now() - new Date(trace.created_at).getTime()) / 1000))} 秒` : "-";

export function AiLivePanel({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false); const [traces, setTraces] = useState<Trace[]>([]); const [collapsed, setCollapsed] = useState(false);
  const load = async () => {
    try {
      const [execution, autoRuns] = await Promise.all([
        api(`/projects/${projectId}/execution-traces?limit=18`) as Promise<Trace[]>,
        api(`/projects/${projectId}/auto-director/runs`).catch(() => []) as Promise<AutoRun[]>,
      ]);
      const autoTraces = (autoRuns || []).flatMap((run) => (run.steps || []).map((step) => ({
        id: `auto-${step.id}`,
        stage: step.stage,
        status: step.status === "COMMITTED" ? "SUCCEEDED" : step.status === "FAILED" || step.status === "BLOCKED" ? step.status : run.status === "RUNNING" && step.stage === run.current_stage ? "STARTED" : step.status,
        model: step.usage_metrics?.model,
        latency_ms: step.usage_metrics?.latency_ms,
        error_code: step.error_code,
        created_at: step.started_at || run.updated_at,
        source_type: "AUTO_DIRECTOR",
      } as Trace)));
      setTraces([...execution, ...autoTraces].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))).slice(0, 24));
    } catch { /* A missing log must not interrupt writing. */ }
  };
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 2200); return () => window.clearInterval(timer); }, [projectId]);
  const active = useMemo(() => traces.filter((trace) => trace.status === "STARTED"), [traces]);
  return <>
    <button className="ai-live-trigger" onClick={() => setOpen(true)} title="查看 AI 创作实况"><Radio size={15} className={active.length ? "ai-live-pulse" : ""} /><span>AI 实况</span>{active.length > 0 && <b>{active.length}</b>}</button>
    {open && <aside className="ai-live-panel" aria-label="AI 创作实况"><header><div><div className="ai-live-title"><Radio size={15} />AI 创作实况</div><small>{active.length ? `${active.length} 项正在处理` : "调用历史与实时阶段"}</small></div><div className="ai-live-actions"><button onClick={() => setCollapsed((value) => !value)} title={collapsed ? "展开记录" : "收起记录"}><ChevronDown size={16} className={collapsed ? "ai-live-chevron-collapsed" : ""} /></button><button onClick={() => setOpen(false)} title="关闭"><X size={16} /></button></div></header>{!collapsed && <div className="ai-live-list">{traces.length ? traces.map((trace) => <article key={trace.id} className={`ai-live-call ai-live-${String(trace.status || "").toLowerCase()}`}><div className="ai-live-call-head"><span className="ai-live-status">{trace.status === "STARTED" ? <LoaderCircle size={16} className="ai-live-spin" /> : trace.status === "SUCCEEDED" ? <CheckCircle2 size={16} /> : trace.status === "FAILED" || trace.status === "BLOCKED" ? <AlertTriangle size={16} /> : <CircleDot size={16} />}</span><div><strong>{stageText(trace.stage, trace.source_type)}</strong><p>{trace.status === "FAILED" ? `需要检查：${trace.error_code || "模型返回失败"}` : trace.status === "BLOCKED" ? "已暂停，等待作者处理" : trace.validation_report?.live_message || (trace.status === "SUCCEEDED" ? "调用已完成" : "正在等待模型返回…")}</p></div><time>{timeText(trace.created_at)}</time></div><div className="ai-live-meta"><span><Layers3 size={12} />{trace.validation_report?.live_phase || statusText(trace.status)}</span><span><Clock3 size={12} />{elapsedText(trace)}</span><span><FileText size={12} />{trace.model || "本地整理"}</span></div>{trace.validation_report?.generation_warning && <div className="ai-live-note">已保留作者大纲，模型部分可稍后深化</div>}</article>) : <div className="ai-live-empty"><Radio size={20} /><p>还没有 AI 工作记录<br /><small>开始生成规划或章节后，这里会自动出现。</small></p></div>}</div>}</aside>}
  </>;
}
