"use client";

import { useEffect, useState } from "react";
import { Bot, CheckCircle2, Play, XCircle } from "lucide-react";
import { api } from "../../../lib";
import { DeveloperData, EmptyState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";

type Run = { context_summary: Record<string, unknown>; proposal: { id: string; participants: string[]; status: string; title?: string }; validation_report: { valid: boolean; issues: unknown[] } };

export default function DirectorPage({ params }: { params: { id: string } }) {
  const [overview, setOverview] = useState<any>();
  const [run, setRun] = useState<Run>();
  const [characterId, setCharacterId] = useState("");
  const [characterRun, setCharacterRun] = useState<any>();
  const [performance, setPerformance] = useState<any>();
  const [message, setMessage] = useState("");

  useEffect(() => { void api(`/projects/${params.id}/snapshot`).then(setOverview); }, [params.id]);
  async function dryRun() { const result = await api(`/projects/${params.id}/director/dry-run`, { method: "POST" }) as Run; setRun(result); setCharacterId(result.proposal.participants[0] || ""); setCharacterRun(undefined); }
  async function simulate(ai: boolean) { if (run && characterId) setCharacterRun(await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/characters/${characterId}/${ai ? "ai-dry-run" : "dry-run"}`, { method: "POST" })); }
  async function transition(action: "approve" | "reject") { if (!run) return; const result = await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/${action}`, { method: "POST", body: action === "reject" ? JSON.stringify({ reason: "由导演台拒绝。" }) : undefined }); setMessage(action === "approve" ? `提案已批准：${result.proposal.status}` : `提案已拒绝：${result.status}`); if (action === "approve") setRun({ ...run, proposal: result.proposal, validation_report: result.validation_report }); }
  async function createPerformance(mode: "HEURISTIC" | "LLM") { if (run) setPerformance(await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/performances`, { method: "POST", body: JSON.stringify({ mode, max_turns: 6 }) })); }
  async function stepPerformance() { if (performance) setPerformance((await api(`/projects/${params.id}/performances/${performance.id}/step`, { method: "POST" })).performance); }
  async function resolveWorld(mode: "HEURISTIC" | "LLM") { if (performance) setPerformance((await api(`/projects/${params.id}/performances/${performance.id}/world/resolve`, { method: "POST", body: JSON.stringify({ mode }) })).performance); }

  return <main className="stack">
    <PageHeader title="导演台" description="推演下一场的候选方案。这里不会创建正式场景，也不会直接改写世界。" action={<button onClick={dryRun}><Play size={15} /> 推演下一场</button>} />
    {message && <SectionCard><p>{message}</p></SectionCard>}
    {!run && <EmptyState title="尚未开始推演" description="导演会基于当前人物、世界事实和剧情线程生成一份待审核的场景提案。" />}
    {run && <section className="stack">
      <SectionCard title="候选场景" aside={<StatusBadge value={run.proposal.status} />}>
        <p><strong>{run.proposal.title || "未命名场景提案"}</strong></p>
        <p className="muted">参与人物：{run.proposal.participants.length || "未指定"} 位</p>
        <div className="row"><button disabled={!run.validation_report.valid || run.proposal.status === "APPROVED"} onClick={() => transition("approve")}><CheckCircle2 size={15} /> 批准提案</button><button className="secondary" onClick={() => transition("reject")}><XCircle size={15} /> 拒绝提案</button></div>
        <DeveloperData value={run.proposal} label="提案原始数据" />
      </SectionCard>
      <SectionCard title="角色选择模拟" description="使用已批准的场景提案检查角色下一步的候选行动。">
        <div className="row"><select value={characterId} onChange={(event) => setCharacterId(event.target.value)}>{run.proposal.participants.map((id) => <option key={id} value={id}>{id}</option>)}</select><button className="secondary" disabled={!characterId} onClick={() => simulate(false)}>规则模拟</button><button disabled={!characterId} onClick={() => simulate(true)}><Bot size={15} /> AI 模拟</button></div>
        {characterRun && <DeveloperData value={characterRun} label="角色模拟数据" />}
      </SectionCard>
      <SectionCard title="场景排演" description="排演属于临时工作区；有效候选不会直接成为正式世界历史。">
        {!performance ? <div className="row"><button disabled={run.proposal.status !== "APPROVED"} onClick={() => createPerformance("HEURISTIC")}>创建规则排演</button><button className="secondary" disabled={run.proposal.status !== "APPROVED"} onClick={() => createPerformance("LLM")}>创建 AI 排演</button></div> : <div className="stack"><p>当前演出 <StatusBadge value={performance.status} /></p><div className="row"><button disabled={performance.status !== "READY" && performance.status !== "RUNNING"} onClick={stepPerformance}>演下一回合</button>{performance.status === "AWAITING_WORLD" && <><button className="secondary" onClick={() => resolveWorld("HEURISTIC")}>规则世界裁定</button><button onClick={() => resolveWorld("LLM")}>AI 世界裁定</button></>}</div><DeveloperData value={{ turns: performance.turns || [], world_resolutions: performance.world_resolutions || [] }} label="排演数据" /></div>}
      </SectionCard>
      <DeveloperData value={{ context_summary: run.context_summary, validation_report: run.validation_report, overview }} label="导演上下文与验证数据" />
    </section>}
  </main>;
}
