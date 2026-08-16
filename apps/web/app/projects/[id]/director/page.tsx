"use client";
import { useEffect, useState } from "react";
import { api } from "../../../lib";

type Run = { context_summary: any; proposal: any; validation_report: { valid: boolean; issues: any[] } };

export default function DirectorPage({ params }: { params: { id: string } }) {
  const [overview, setOverview] = useState<any>();
  const [run, setRun] = useState<Run>();
  const [message, setMessage] = useState("");
  useEffect(() => { void api(`/projects/${params.id}/snapshot`).then(setOverview); }, [params.id]);
  async function dryRun() { setMessage(""); const result = await api(`/projects/${params.id}/director/dry-run`, { method: "POST" }) as Run; setRun(result); }
  async function transition(action: "approve" | "reject") { if (!run) return; const result = await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/${action}`, { method: "POST", body: action === "reject" ? JSON.stringify({ reason: "Rejected from Director page." }) : undefined }); setMessage(action === "approve" ? `已批准提案：${result.proposal.status}` : `已拒绝提案：${result.status}`); if (action === "approve") setRun({ ...run, proposal: result.proposal, validation_report: result.validation_report }); }
  return <main className="stack"><section><h1>导演</h1><p className="muted">仅推演下一场，不创建 Scene 或修改世界状态。</p></section>{overview && <section className="grid cards"><div className="panel"><h2>当前 Story Arc</h2><pre>{JSON.stringify(overview.current_story_arc, null, 2)}</pre></div><div className="panel"><h2>活跃 Story Threads</h2><pre>{JSON.stringify(overview.active_story_threads, null, 2)}</pre></div><div className="panel"><h2>主要人物当前目标</h2><pre>{JSON.stringify(overview.character_states.map((state: any) => ({ character_id: state.character_id, goals: state.goals })), null, 2)}</pre></div></section>}<button onClick={dryRun}>推演下一场</button>{message && <p>{message}</p>}{run && <section className="stack"><section className="panel"><h2>推荐场景</h2><div className="grid cards"><div><h3>参与人物</h3><pre>{JSON.stringify(run.proposal.participants, null, 2)}</pre></div><div><h3>地点</h3><p>{run.proposal.proposed_location || "待定"}</p></div><div><h3>场景目标</h3><p>{run.proposal.scene_goal}</p></div><div><h3>预计推进</h3><pre>{JSON.stringify(run.proposal.expected_progress, null, 2)}</pre></div></div><h3>为什么建议这一场</h3><p>{run.proposal.director_reasoning_summary}</p><h3>可能结果</h3><pre>{JSON.stringify(run.proposal.possible_outcomes, null, 2)}</pre><h3>风险</h3><pre>{JSON.stringify(run.proposal.risk_flags, null, 2)}</pre></section><section className="panel"><h2>Validation Report：{run.validation_report.valid ? "可批准" : "存在阻断问题"}</h2><pre>{JSON.stringify(run.validation_report.issues, null, 2)}</pre></section><div className="project-nav"><button disabled={!run.validation_report.valid || run.proposal.status === "APPROVED"} onClick={() => transition("approve")}>Approve</button><button onClick={() => transition("reject")}>Reject</button></div></section>}</main>;
}
