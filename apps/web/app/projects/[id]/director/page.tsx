"use client";
import { useEffect, useState } from "react";
import { api } from "../../../lib";

type Run = { context_summary: any; proposal: any; validation_report: { valid: boolean; issues: any[] } };

export default function DirectorPage({ params }: { params: { id: string } }) {
  const [overview, setOverview] = useState<any>(); const [run, setRun] = useState<Run>();
  const [characterId, setCharacterId] = useState(""); const [characterRun, setCharacterRun] = useState<any>();
  const [performance, setPerformance] = useState<any>(); const [message, setMessage] = useState("");
  useEffect(() => { void api(`/projects/${params.id}/snapshot`).then(setOverview); }, [params.id]);
  async function dryRun() { const result = await api(`/projects/${params.id}/director/dry-run`, { method: "POST" }) as Run; setRun(result); setCharacterId(result.proposal.participants[0] || ""); setCharacterRun(undefined); }
  async function simulate(ai: boolean) { if (!run || !characterId) return; setCharacterRun(await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/characters/${characterId}/${ai ? "ai-dry-run" : "dry-run"}`, { method: "POST" })); }
  async function transition(action: "approve" | "reject") { if (!run) return; const result = await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/${action}`, { method: "POST", body: action === "reject" ? JSON.stringify({ reason: "Rejected from Director page." }) : undefined }); setMessage(action === "approve" ? `已批准提案：${result.proposal.status}` : `已拒绝提案：${result.status}`); if (action === "approve") setRun({ ...run, proposal: result.proposal, validation_report: result.validation_report }); }
  async function createPerformance(mode: "HEURISTIC" | "LLM") { if (run) setPerformance(await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/performances`, { method: "POST", body: JSON.stringify({ mode, max_turns: 6 }) })); }
  async function stepPerformance() { if (performance) setPerformance((await api(`/projects/${params.id}/performances/${performance.id}/step`, { method: "POST" })).performance); }
  async function resolveWorld(mode: "HEURISTIC" | "LLM") { if (performance) setPerformance((await api(`/projects/${params.id}/performances/${performance.id}/world/resolve`, { method: "POST", body: JSON.stringify({ mode }) })).performance); }
  return <main className="stack">
    <section><h1>导演</h1><p className="muted">仅推演下一场，不创建 Scene 或修改世界状态。</p></section>
    {overview && <section className="grid cards"><div className="panel"><h2>当前 Story Arc</h2><pre>{JSON.stringify(overview.current_story_arc, null, 2)}</pre></div><div className="panel"><h2>活跃 Story Threads</h2><pre>{JSON.stringify(overview.active_story_threads, null, 2)}</pre></div><div className="panel"><h2>主要人物当前目标</h2><pre>{JSON.stringify(overview.character_states.map((state: any) => ({ character_id: state.character_id, goals: state.goals })), null, 2)}</pre></div></section>}
    <button onClick={dryRun}>推演下一场</button>{message && <p>{message}</p>}
    {run && <section className="stack"><section className="panel"><h2>推荐场景</h2><pre>{JSON.stringify(run.proposal, null, 2)}</pre></section>
      <section className="panel"><h2>模拟角色选择</h2><div className="row"><select value={characterId} onChange={(event) => setCharacterId(event.target.value)}>{run.proposal.participants.map((id: string) => <option key={id} value={id}>{id}</option>)}</select><button disabled={!characterId} onClick={() => simulate(false)}>规则模拟</button><button disabled={!characterId} onClick={() => simulate(true)}>AI模拟</button></div>{characterRun && <pre>{JSON.stringify(characterRun, null, 2)}</pre>}</section>
      <section className="panel"><h2>Scene Rehearsal</h2><div className="row"><button disabled={run.proposal.status !== "APPROVED"} onClick={() => createPerformance("HEURISTIC")}>创建规则排演</button><button disabled={run.proposal.status !== "APPROVED"} onClick={() => createPerformance("LLM")}>创建AI排演</button></div>{performance && <div className="stack"><p>Take #{performance.take_number} · {performance.status}</p><button disabled={performance.status !== "READY" && performance.status !== "RUNNING"} onClick={stepPerformance}>演下一回合</button>{performance.status === "AWAITING_WORLD" && <div className="row"><button onClick={() => resolveWorld("HEURISTIC")}>规则世界裁定</button><button onClick={() => resolveWorld("LLM")}>AI世界裁定</button></div>}<pre>{JSON.stringify({ turns: performance.turns || [], world_resolutions: performance.world_resolutions || [] }, null, 2)}</pre></div>}</section>
      <section className="panel"><h2>Validation Report</h2><pre>{JSON.stringify(run.validation_report, null, 2)}</pre></section><div className="project-nav"><button disabled={!run.validation_report.valid || run.proposal.status === "APPROVED"} onClick={() => transition("approve")}>Approve</button><button onClick={() => transition("reject")}>Reject</button></div>
    </section>}
  </main>;
}
