"use client";

import { useEffect, useState } from "react";
import { Archive, BookOpen, Lock } from "lucide-react";
import { api, ApiError } from "../../../lib";
import { ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";

type Volume = { id: string; volume_number: number; title?: string; status: string; progress?: Record<string, any>; next_volume_hooks?: unknown[] };
export default function ContinuityPage({ params }: { params: { id: string } }) {
  const [volumes, setVolumes] = useState<Volume[]>([]); const [error, setError] = useState(""); const [snapshot, setSnapshot] = useState<Record<string, any>>();
  useEffect(() => { void api(`/projects/${params.id}/volumes`).then((value: unknown) => setVolumes(value as Volume[])).catch((reason: unknown) => setError(reason instanceof ApiError ? reason.message : "无法读取跨卷连续性")); }, [params.id]);
  async function read(volume: Volume) { try { setSnapshot(await api(`/projects/${params.id}/volumes/${volume.id}/continuity`) as Record<string, any>); } catch (reason: unknown) { setError(reason instanceof ApiError ? reason.message : "无法读取封存快照"); } }
  return <main className="stack"><PageHeader title="跨卷连续性" description="封存卷以不可变快照提供后续卷读取；角色未知信息和导演内部推理不会进入正文上下文。" />{error && <ErrorState message={error} />}{!volumes.length && !error ? <LoadingState /> : <><div className="library-grid">{volumes.map((volume) => <button type="button" className="world-card" key={volume.id} onClick={() => void read(volume)}><div className="world-card-top"><span><Archive size={15} />第 {volume.volume_number} 卷</span><StatusBadge value={volume.status} /></div><h3>{volume.title || "未命名卷"}</h3><p>{volume.status === "SEALED" ? "可读取封存快照" : "当前卷仍在变化，暂无不可变快照"}</p></button>)}</div>{snapshot && <SectionCard title={snapshot.volume?.title || "连续性快照"}><div className="task-summary"><div><small>摘要</small><p>{snapshot.snapshot?.summary || "暂无"}</p></div><div><small>活跃线程</small><p>{(snapshot.snapshot?.active_threads || []).join("、") || "无"}</p></div><div><small>未回收伏笔</small><p>{(snapshot.snapshot?.unresolved_foreshadowings || []).join("、") || "无"}</p></div><div><small>后续卷承接点</small><p>{(snapshot.snapshot?.next_volume_hooks || []).join("、") || "无"}</p></div></div><p className="muted"><Lock size={13} /> 未来禁止揭示：{(snapshot.snapshot?.forbidden_future_reveals || []).join("、") || "无"}</p></SectionCard>}</>}</main>;
}
