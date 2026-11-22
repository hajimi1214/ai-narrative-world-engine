"use client";

import { useEffect, useState } from "react";
import { Archive, BookOpen, Lock } from "lucide-react";
import { api, ApiError } from "../../../lib";
import { ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";
import { AutoCrossVolumeSetting } from "../../../../components/author/auto-cross-volume-setting";

type Volume = { id: string; volume_number: number; title?: string; status: string; progress?: Record<string, any>; next_volume_hooks?: unknown[] };
const list = (value: unknown): any[] => Array.isArray(value) ? value : [];
const mapEntries = (value: unknown): Array<[string, any]> => value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value as Record<string, any>) : [];
const recordLabel = (item: any) => item.label || item.title || item.foreshadow_ref || item.event_ref || item.id || "未命名记录";
export default function ContinuityPage({ params }: { params: { id: string } }) {
  const [volumes, setVolumes] = useState<Volume[]>([]); const [error, setError] = useState(""); const [snapshot, setSnapshot] = useState<Record<string, any>>();
  useEffect(() => { void api(`/projects/${params.id}/volumes`).then((value: unknown) => setVolumes(value as Volume[])).catch((reason: unknown) => setError(reason instanceof ApiError ? reason.message : "无法读取跨卷连续性")); }, [params.id]);
  async function read(volume: Volume) { try { setSnapshot(await api(`/projects/${params.id}/volumes/${volume.id}/continuity`) as Record<string, any>); } catch (reason: unknown) { setError(reason instanceof ApiError ? reason.message : "无法读取封存快照"); } }
  return <main className="stack">
    <PageHeader title="跨卷连续性" description="封存卷以不可变快照提供后续卷读取；角色未知信息和导演内部推理不会进入正文上下文。" />
    {error && <ErrorState message={error} />}
    <AutoCrossVolumeSetting projectId={params.id} />
    {!volumes.length && !error ? <LoadingState /> : <>
      <div className="library-grid">{volumes.map((volume) => <button type="button" className="world-card" key={volume.id} onClick={() => void read(volume)}>
        <div className="world-card-top"><span><Archive size={15} />第 {volume.volume_number} 卷</span><StatusBadge value={volume.status} /></div>
        <h3>{volume.title || "未命名卷"}</h3>
        <p>{volume.status === "SEALED" ? "可读取封存快照" : "当前卷仍在变化，暂无不可变快照"}</p>
      </button>)}</div>
      {snapshot && <SectionCard title={snapshot.volume?.title || "连续性快照"}>
        <div className="task-summary">
          <div><small>摘要</small><p>{snapshot.snapshot?.summary || "暂无"}</p></div>
          <div><small>采用章节</small><p>{list(snapshot.snapshot?.timeline_end?.adopted_chapters).map((item) => `第 ${item.chapter_number} 章 · ${item.content_fingerprint}`).join("、") || "无"}</p></div>
          <div><small>正式世界指纹</small><p>{snapshot.snapshot?.timeline_end?.formal_world_fingerprint || "缺少"}</p></div>
          <div><small>时间线末端</small><p>{snapshot.snapshot?.timeline_end?.world_time || "无世界时间"} · 序号 {snapshot.snapshot?.timeline_end?.end_sequence ?? "-"}</p></div>
        </div>
        <div className="stack" style={{ marginTop: 18 }}>
          <details open><summary>已核实剧情事实（{list(snapshot.snapshot?.confirmed_facts).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.confirmed_facts).map((item, index) => <article className="resource-row" key={item.requirement_ref || `${item.scene_id || "fact"}-${index}`}><strong className="resource-title">{recordLabel(item)}</strong><span>{item.excerpt || item.fact?.predicate || "已发布事实"}</span><small>第 {item.chapter_number ?? "-"} 章 · 审核 {item.assessment_id || "场景记录"}</small><small>{item.content_fingerprint || item.scene_id || ""}</small></article>)}</div></details>
          <details><summary>发布场景与检查点（{list(snapshot.snapshot?.timeline_end?.published_scenes).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.timeline_end?.published_scenes).map((item) => <article className="resource-row" key={item.scene_id}><strong className="resource-title">第 {item.chapter_number} 章 · 场景 {item.sequence}</strong><span>{item.world_time || "未设世界时间"} · 地点 {item.location_id || "未记录"}</span><small>提交 {item.scene_commit_id}</small><small>检查点 {item.checkpoint_id} · {item.checkpoint_fingerprint}</small><small>正式世界 {item.pre_world_fingerprint} → {item.post_world_fingerprint}</small></article>)}</div></details>
          <details><summary>时间线事件（{list(snapshot.snapshot?.timeline_end?.timeline_events).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.timeline_end?.timeline_events).map((item) => <article className="resource-row" key={item.timeline_event_id}><strong className="resource-title">{item.event_type} · {item.sequence ?? "-"}.{item.ordinal ?? "-"}</strong><span>场景 {item.scene_id || "-"} · {item.world_time || "无世界时间"}</span><small>{item.target_type && item.target_id ? `${item.target_type} ${item.target_id} ${item.path || ""}` : item.source_type}</small><small>{JSON.stringify(item.before_value)} → {JSON.stringify(item.after_value)}</small><small>{item.event_fingerprint}</small></article>)}</div></details>
          <details><summary>人物状态（{mapEntries(snapshot.snapshot?.character_states).length}）</summary><div className="resource-list">{mapEntries(snapshot.snapshot?.character_states).map(([id, item]) => <article className="resource-row" key={id}><strong className="resource-title">{item.name || id}</strong><span>当前状态 {JSON.stringify(item.current_state || {})}</span><small>身体 {JSON.stringify(item.physical_state || {})} · 情绪 {JSON.stringify(item.emotional_state || {})}</small><small>关系 {JSON.stringify(item.relationships || {})}</small><small>已知事实 {list(item.known_facts_at_seal).map((fact) => fact.proposition).join("、") || "无"}</small></article>)}</div></details>
          <details><summary>关系变化（{list(snapshot.snapshot?.relationship_changes).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.relationship_changes).map((item) => <article className="resource-row" key={item.event_id}><strong className="resource-title">{item.character_id} · {item.path}</strong><span>{JSON.stringify(item.before_value)} → {JSON.stringify(item.after_value)}</span><small>第 {item.chapter_number} 章 · 场景 {item.scene_id}</small><small>时间线事件 {item.event_id} · {item.event_fingerprint}</small></article>)}</div></details>
          <details><summary>地点状态（{mapEntries(snapshot.snapshot?.location_states).length}）</summary><div className="resource-list">{mapEntries(snapshot.snapshot?.location_states).map(([id, item]) => <article className="resource-row" key={id}><strong className="resource-title">{item.name || id}</strong><span>{JSON.stringify(item.profile || {})}</span><small>正式世界地点 ID {id}</small></article>)}</div></details>
          <details><summary>物品状态（{mapEntries(snapshot.snapshot?.item_states).length}）</summary><div className="resource-list">{mapEntries(snapshot.snapshot?.item_states).map(([id, item]) => <article className="resource-row" key={id}><strong className="resource-title">{item.name || id}</strong><span>{JSON.stringify(item.profile || {})}</span><small>正式世界物品 ID {id}</small></article>)}</div></details>
          <details><summary>故事线程（{list(snapshot.snapshot?.active_threads).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.active_threads).map((item) => <article className="resource-row" key={item.thread_id}><strong className="resource-title">{item.title || item.thread_id}</strong><StatusBadge value={item.status} /><span>进度 {item.progress}</span><small>{item.goal || item.type}</small><small>线程状态 {JSON.stringify(item.state || {})}</small></article>)}</div></details>
          <details><summary>伏笔回收证据（{list(snapshot.snapshot?.paid_off_foreshadowings).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.paid_off_foreshadowings).map((item) => <article className="resource-row" key={item.foreshadowing_id || item.foreshadow_ref}><strong className="resource-title">{item.title || item.foreshadow_ref}</strong><span>{item.evidence?.excerpt || "无正文引用"}</span><small>第 {item.chapter_number ?? "-"} 章 · 审核 {item.assessment_id || "-"}</small><small>{item.content_fingerprint || ""}</small></article>)}</div></details>
          <details><summary>未解决伏笔（{list(snapshot.snapshot?.unresolved_foreshadowings).length}）</summary><div className="resource-list">{list(snapshot.snapshot?.unresolved_foreshadowings).map((item) => <article className="resource-row" key={item.foreshadowing_id || item.foreshadow_ref || item.missing_ref}><strong className="resource-title">{item.title || item.foreshadow_ref || item.missing_ref}</strong><StatusBadge value={item.status} /><span>{item.evidence_status}</span><small>{item.foreshadow_ref || item.missing_ref}</small></article>)}</div></details>
        </div>
        <p className="muted" style={{ marginTop: 16 }}><Lock size={13} /> 未来禁止揭示：{list(snapshot.snapshot?.forbidden_future_reveals).map(recordLabel).join("、") || "无"}</p>
        <p className="muted">快照来源指纹：{snapshot.snapshot?.source_fingerprint || "缺少"}</p>
      </SectionCard>}
    </>}
  </main>;
}
