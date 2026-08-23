"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, BookOpen, Lock, Play, Plus, RotateCcw } from "lucide-react";
import { api, ApiError } from "../../../lib";
import { ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";

type Volume = { id: string; volume_number: number; title?: string; status: string; estimated_chapter_start?: number; estimated_chapter_end?: number; actual_chapter_start?: number; actual_chapter_end?: number; volume_goal?: string; progress?: { volume_goal_progress?: number; should_prepare_seal?: boolean; unresolved_threads?: unknown[] }; windows?: Array<{ id: string; start_chapter_number: number; end_chapter_number: number; status: string }> };

export default function VolumesPage({ params }: { params: { id: string } }) {
  const [volumes, setVolumes] = useState<Volume[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const load = () => { void api(`/projects/${params.id}/volumes`).then((value: unknown) => setVolumes(value as Volume[])).catch((reason: unknown) => setError(reason instanceof ApiError ? reason.message : "无法读取卷总览")); };
  useEffect(load, [params.id]);
  async function start(volume: Volume) { setBusy(true); try { await api(`/projects/${params.id}/volumes/${volume.id}/continue`, { method: "POST", body: JSON.stringify({}) }); load(); } catch (reason: unknown) { setError(reason instanceof ApiError ? reason.message : "无法继续本卷"); } finally { setBusy(false); } }
  return <main className="stack"><PageHeader title="卷总览" description="预估范围只用于规划参考；实际卷长和章节数由剧情完成条件与作者确认决定。" action={<Link className="button secondary" href={`/projects/${params.id}/director/volume`}><BookOpen size={15} />当前卷导演</Link>} />{error && <ErrorState message={error} retry={() => { setError(""); load(); }} />}{!volumes.length && !error ? <LoadingState /> : <div className="stack">{volumes.map((volume) => <SectionCard key={volume.id} title={`第 ${volume.volume_number} 卷 · ${volume.title || "未命名"}`} aside={<StatusBadge value={volume.status} />}><div className="run-metrics"><span>预估范围：{volume.estimated_chapter_start || "-"} - {volume.estimated_chapter_end || "-"}</span><span>实际范围：{volume.actual_chapter_start || "-"} - {volume.actual_chapter_end || "-"}</span><span>目标进度：{Math.round((volume.progress?.volume_goal_progress || 0) * 100)}%</span><span>未回收线程：{volume.progress?.unresolved_threads?.length || 0}</span></div><p className="muted">{volume.volume_goal || "当前卷尚未填写目标。"}</p><div className="row">{volume.status !== "SEALED" ? <button onClick={() => void start(volume)} disabled={busy}><Play size={15} />继续本卷</button> : <span className="muted"><Lock size={14} />已封存，仅可读取快照</span>}<Link className="button secondary" href={`/projects/${params.id}/director/volume?volume=${volume.id}`}>查看导演台 <ArrowRight size={15} /></Link>{volume.progress?.should_prepare_seal && <span className="status-badge status-valid">可申请封存</span>}</div>{volume.windows?.length ? <div className="completed-chapters">{volume.windows.map((window) => <span key={window.id}>窗口 {window.start_chapter_number}-{window.end_chapter_number} · {window.status}</span>)}</div> : null}</SectionCard>)}</div>}</main>;
}
