"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowRight, BookOpen, Lock, Play, Plus, RotateCcw } from "lucide-react";
import { api, ApiError } from "../../../lib";
import { ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";
import { createLivePoller, type LiveSyncState } from "../../../../components/live/live-poller";

type Volume = { id: string; volume_number: number; title?: string; status: string; estimated_chapter_start?: number; estimated_chapter_end?: number; actual_chapter_start?: number; actual_chapter_end?: number; volume_goal?: string; progress?: { volume_goal_progress?: number; should_prepare_seal?: boolean; unresolved_threads?: unknown[] }; windows?: Array<{ id: string; start_chapter_number: number; end_chapter_number: number; status: string }> };

export default function VolumesPage({ params }: { params: { id: string } }) {
  const [volumes, setVolumes] = useState<Volume[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [message, setMessage] = useState("");
  const pollerRef = useRef<ReturnType<typeof createLivePoller> | null>(null);
  const [sync, setSync] = useState<LiveSyncState>({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 });
  useEffect(() => {
    let cancelled = false;
    const poller = createLivePoller({
      intervalMs: 2000,
      load: async (signal) => {
        const value: unknown = await api(`/projects/${params.id}/volumes`, { signal });
        if (signal.aborted || cancelled) return;
        if (!Array.isArray(value)) throw new Error("INVALID_VOLUMES_RESPONSE");
        setVolumes(value as Volume[]);
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
  }, [params.id]);
  function showActionError(reason: unknown, fallback: string) {
    setError(reason instanceof ApiError ? reason.message : fallback);
  }
  async function action(volume: Volume, path: "start" | "continue" | "extend" | "seal", body: Record<string, unknown> = {}) { setBusy(true); setError(""); try { await api(`/projects/${params.id}/volumes/${volume.id}/${path}`, { method: "POST", body: JSON.stringify(body) }); pollerRef.current?.refresh(); } catch (reason: unknown) { showActionError(reason, "卷操作失败"); } finally { setBusy(false); } }
  async function startNextVolume(volume: Volume) { setBusy(true); setError(""); setMessage(""); try { const result = await api(`/projects/${params.id}/volumes/${volume.id}/next`, { method: "POST" }) as { volume: { id: string; volume_number: number } }; await api(`/projects/${params.id}/volumes/${result.volume.id}/start`, { method: "POST" }); setMessage(`第 ${result.volume.volume_number} 卷已开始，继续推进后续章节。`); pollerRef.current?.refresh(); } catch (reason: unknown) { showActionError(reason, "无法开始下一卷"); } finally { setBusy(false); } }
  const syncLabel = sync.status === "paused" ? "后台暂停同步" : sync.status === "syncing" ? "正在同步卷状态" : sync.status === "offline" ? "连接暂断，自动重试中" : "卷状态已同步";
  const lastUpdated = sync.lastUpdatedAt ? new Date(sync.lastUpdatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "尚未同步";
  return <main className="stack"><PageHeader title="卷总览" description="预估范围只用于规划参考；实际卷长和章节数由剧情完成条件与作者确认决定。" action={<Link className="button secondary" href={`/projects/${params.id}/director/volume`}><BookOpen size={15} />当前卷导演</Link>} /><div className={`ai-live-sync${sync.status === "offline" ? " ai-live-sync-offline" : ""}`} aria-live="polite"><span>{syncLabel}{sync.status === "offline" && volumes.length > 0 ? " · 保留已显示数据" : ""}</span><span className="ai-live-sync-meta">最近更新：{lastUpdated}</span><button className="ai-live-refresh" onClick={() => pollerRef.current?.refresh()} disabled={sync.status === "paused" || sync.status === "syncing"} aria-label={sync.status === "offline" ? "立即重试同步卷状态" : "刷新卷状态"}><RotateCcw size={14} />{sync.status === "offline" ? "立即重试" : "刷新"}</button></div>{error && <ErrorState message={error} retry={() => { setError(""); pollerRef.current?.refresh(); }} />}{message && <div className="success-state" role="status">{message}</div>}{!volumes.length && !sync.lastUpdatedAt && sync.status !== "offline" ? <LoadingState /> : !volumes.length ? <p className="muted">{sync.status === "offline" ? "暂时无法同步卷总览，请重试。" : "当前还没有卷。"}</p> : <div className="stack">{volumes.map((volume) => <SectionCard key={volume.id} title={`第 ${volume.volume_number} 卷 · ${volume.title || "未命名"}`} aside={<StatusBadge value={volume.status} />}><div className="run-metrics"><span>预估范围：{volume.estimated_chapter_start || "-"} - {volume.estimated_chapter_end || "-"}</span><span>实际范围：{volume.actual_chapter_start || "-"} - {volume.actual_chapter_end || "-"}</span><span>目标进度：{Math.round((volume.progress?.volume_goal_progress || 0) * 100)}%</span><span>未回收线程：{volume.progress?.unresolved_threads?.length || 0}</span></div><p className="muted">{volume.volume_goal || "当前卷尚未填写目标。"}</p><div className="row">
  {volume.status !== "SEALED" && <button onClick={() => void action(volume, volume.status === "DRAFT" ? "start" : "continue")} disabled={busy}><Play size={15} />{volume.status === "DRAFT" ? "开始本卷" : "继续本卷"}</button>}
  {volume.status !== "SEALED" && <button className="secondary" onClick={() => void action(volume, "extend", { window_size: 5 })} disabled={busy}><Plus size={15} />扩展当前卷</button>}
  {volume.progress?.should_prepare_seal && volume.status !== "SEALED" && <button className="secondary" onClick={() => void action(volume, "seal", { author_confirmed: true })} disabled={busy}><Lock size={15} />封存本卷</button>}
  {volume.status === "SEALED" && <><span className="muted"><Lock size={14} />已封存，仅可读取快照</span><button className="secondary" onClick={() => void startNextVolume(volume)} disabled={busy}><ArrowRight size={15} />开始下一卷并继续写作</button></>}
  <Link className="button secondary" href={"/projects/" + params.id + "/director/volume?volume=" + volume.id}>查看导演台 <ArrowRight size={15} /></Link>
</div>{volume.windows?.length ? <div className="completed-chapters">{volume.windows.map((window) => <span key={window.id}>窗口 {window.start_chapter_number}-{window.end_chapter_number} · {window.status}</span>)}</div> : null}</SectionCard>)}</div>}</main>;
}
