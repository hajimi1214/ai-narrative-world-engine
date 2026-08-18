"use client";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../../../../lib";

export default function ReplayWorkspace({ params }: { params: { id: string; sessionId: string } }) {
  const client = useQueryClient(); const [confirm, setConfirm] = useState(false);
  const query = useQuery({ queryKey: ["replay-session", params.sessionId], queryFn: () => api(`/projects/${params.id}/retcon/replay-sessions/${params.sessionId}`) as Promise<any> });
  const step = useMutation({ mutationFn: () => api(`/projects/${params.id}/retcon/replay-sessions/${params.sessionId}/step`, { method: "POST" }), onSuccess: () => client.invalidateQueries({ queryKey: ["replay-session", params.sessionId] }) });
  const commit = useMutation({ mutationFn: () => api(`/projects/${params.id}/retcon/replay-sessions/${params.sessionId}/commit`, { method: "POST", body: JSON.stringify({ explicit_confirmation: true }) }), onSuccess: () => { setConfirm(false); client.invalidateQueries({ queryKey: ["replay-session", params.sessionId] }); } });
  if (query.isLoading) return <main className="stack"><p>正在读取历史重演状态……</p></main>;
  if (query.isError) return <main className="stack"><section className="preview-warning">暂时无法读取历史重演状态。{(query.error as ApiError).message}</section></main>;
  const data = query.data; const total = data.queue?.length || 0; const ready = data.cursor >= total && data.status === "RUNNING";
  return <main className="stack"><header className="page-header"><div><h1>历史重演</h1><p>按照冻结的因果计划逐场验证和重演历史。</p></div><span className="status-badge">{data.status === "BLOCKED" ? "重演已阻断" : `${data.cursor} / ${total}`}</span></header>
    {data.status === "BLOCKED" && <section className="preview-warning"><strong>历史重演已停止</strong><p>{data.failure_report?.code || "需要人工检查"}</p></section>}
    <section className="section-card"><h2>重演队列</h2><div className="resource-list">{(data.queue || []).map((item: any, index: number) => <article className="resource-row" key={`${item.scene_id}-${index}`}><div><strong>场景 {item.sequence}</strong><small>{item.mode === "REPLAY" ? "重新演出" : "验证保留"} · {item.reason}</small></div><span>{index < data.cursor ? "已完成" : index === data.cursor ? "当前" : "等待"}</span></article>)}</div></section>
    <div className="actions">{!ready && data.status !== "BLOCKED" && <button onClick={() => step.mutate()} disabled={step.isPending}>执行下一步</button>}{ready && <button onClick={() => setConfirm(true)}>采用新的历史</button>}</div>
    {confirm && <div className="dialog-backdrop"><div className="dialog"><h2>确认采用新的历史</h2><p>这会激活新的场景历史，并解除当前世界的等待重演状态。</p><footer><button className="secondary" onClick={() => setConfirm(false)}>取消</button><button onClick={() => commit.mutate()} disabled={commit.isPending}>确认采用</button></footer></div></div>}
  </main>;
}
