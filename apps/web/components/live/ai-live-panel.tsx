"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, CircleDot, LoaderCircle, Radio, X } from "lucide-react";
import { api } from "../../app/lib";

type Trace = { id: string; stage?: string; status?: string; model?: string | null; latency_ms?: number | null; error_code?: string | null; created_at?: string | null; source_type?: string | null };

const stageLabels: Record<string, string> = { WRITER: "正文写作", CRITIC: "章节审核", REPAIR: "问题修复", DIRECTOR: "导演规划", CHARACTER_ACTOR: "角色行动", WORLD_RESOLVER: "世界裁定", SCENE_COMMIT: "保存场景", AUTONOMOUS_LOOP: "自动推进" };
const statusLabels: Record<string, string> = { STARTED: "进行中", SUCCEEDED: "已完成", FAILED: "失败", BLOCKED: "已暂停" };
const stageText = (value?: string) => stageLabels[value || ""] || value || "AI 任务";
const statusText = (value?: string) => statusLabels[value || ""] || value || "等待中";
const timeText = (value?: string | null) => value ? new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "刚刚";

export function AiLivePanel({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false); const [traces, setTraces] = useState<Trace[]>([]); const [collapsed, setCollapsed] = useState(false);
  const load = async () => { try { setTraces((await api(`/projects/${projectId}/execution-traces?limit=18`)) as Trace[]); } catch { /* A missing log must not interrupt writing. */ } };
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 2200); return () => window.clearInterval(timer); }, [projectId]);
  const active = useMemo(() => traces.filter((trace) => trace.status === "STARTED"), [traces]);
  return <>
    <button className="ai-live-trigger" onClick={() => setOpen(true)} title="查看 AI 创作实况"><Radio size={15} className={active.length ? "ai-live-pulse" : ""} /><span>AI 实况</span>{active.length > 0 && <b>{active.length}</b>}</button>
    {open && <aside className="ai-live-panel" aria-label="AI 创作实况"><header><div><div className="ai-live-title"><Radio size={15} />AI 创作实况</div><small>{active.length ? `${active.length} 项正在处理` : "等待下一次 AI 工作"}</small></div><div className="ai-live-actions"><button onClick={() => setCollapsed((value) => !value)} title={collapsed ? "展开记录" : "收起记录"}><ChevronDown size={16} className={collapsed ? "ai-live-chevron-collapsed" : ""} /></button><button onClick={() => setOpen(false)} title="关闭"><X size={16} /></button></div></header>{!collapsed && <div className="ai-live-list">{traces.length ? traces.map((trace) => <article key={trace.id} className={`ai-live-row ai-live-${String(trace.status || "").toLowerCase()}`}><span className="ai-live-status">{trace.status === "STARTED" ? <LoaderCircle size={15} className="ai-live-spin" /> : trace.status === "SUCCEEDED" ? <CheckCircle2 size={15} /> : trace.status === "FAILED" || trace.status === "BLOCKED" ? <AlertTriangle size={15} /> : <CircleDot size={15} />}</span><div><strong>{stageText(trace.stage)}</strong><p>{trace.status === "FAILED" ? `需要检查：${trace.error_code || "模型返回失败"}` : trace.status === "BLOCKED" ? "已暂停，等待作者处理" : trace.status === "SUCCEEDED" ? `${trace.model || "模型"}${trace.latency_ms ? ` · ${trace.latency_ms}ms` : ""}` : "模型正在返回结果…"}</p></div><time>{timeText(trace.created_at)}</time></article>) : <div className="ai-live-empty"><Radio size={20} /><p>还没有 AI 工作记录<br /><small>开始生成规划或章节后，这里会自动出现。</small></p></div>}</div>}</aside>}
  </>;
}
