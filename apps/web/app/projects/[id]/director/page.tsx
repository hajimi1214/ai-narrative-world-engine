"use client";

import { useEffect, useState } from "react";
import { Bot, CheckCircle2, Pause, Play, PlayCircle, RotateCcw, XCircle } from "lucide-react";
import { api } from "../../../lib";
import { DeveloperData, EmptyState, ErrorState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";

type Run = { context_summary: Record<string, unknown>; proposal: { id: string; participants: string[]; status: string; title?: string }; validation_report: { valid: boolean; issues: unknown[] } };
type AutonomyRun = { id: string; status: string; scene_budget: number; committed_scene_count?: number; stop_reason?: string; active?: boolean };

export default function DirectorPage({ params }: { params: { id: string } }) {
  const [overview, setOverview] = useState<any>();
  const [run, setRun] = useState<Run>();
  const [characterId, setCharacterId] = useState("");
  const [characterRun, setCharacterRun] = useState<any>();
  const [performance, setPerformance] = useState<any>();
  const [message, setMessage] = useState(""); const [autonomy, setAutonomy] = useState<AutonomyRun>(); const [busy, setBusy] = useState(false); const [error, setError] = useState("");

  useEffect(() => { void api(`/projects/${params.id}/snapshot`).then(setOverview); void api(`/projects/${params.id}/autonomy/runs`).then((items) => { const active = (items as AutonomyRun[]).find((item) => item.active || ["RUNNING", "PAUSED"].includes(item.status)); if (active) setAutonomy(active); }).catch(() => undefined); }, [params.id]);
  async function dryRun() { const result = await api(`/projects/${params.id}/director/dry-run`, { method: "POST" }) as Run; setRun(result); setCharacterId(result.proposal.participants[0] || ""); setCharacterRun(undefined); }
  async function simulate(ai: boolean) { if (run && characterId) setCharacterRun(await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/characters/${characterId}/${ai ? "ai-dry-run" : "dry-run"}`, { method: "POST" })); }
  async function transition(action: "approve" | "reject") { if (!run) return; const result = await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/${action}`, { method: "POST", body: action === "reject" ? JSON.stringify({ reason: "由导演台拒绝。" }) : undefined }); setMessage(action === "approve" ? `提案已批准：${result.proposal.status}` : `提案已拒绝：${result.status}`); if (action === "approve") setRun({ ...run, proposal: result.proposal, validation_report: result.validation_report }); }
  async function createPerformance(mode: "HEURISTIC" | "LLM") { if (run) setPerformance(await api(`/projects/${params.id}/director/proposals/${run.proposal.id}/performances`, { method: "POST", body: JSON.stringify({ mode, max_turns: 6 }) })); }
  async function stepPerformance() { if (performance) setPerformance((await api(`/projects/${params.id}/performances/${performance.id}/step`, { method: "POST" })).performance); }
  async function resolveWorld(mode: "HEURISTIC" | "LLM") { if (performance) setPerformance((await api(`/projects/${params.id}/performances/${performance.id}/world/resolve`, { method: "POST", body: JSON.stringify({ mode }) })).performance); }
  async function startAutonomy() { setBusy(true); setError(""); try { const created = await api(`/projects/${params.id}/autonomy/runs`, { method: "POST", body: JSON.stringify({ scene_budget: 3, max_turns_per_scene: 6, performance_mode: "HEURISTIC", resolver_mode: "HEURISTIC", idempotency_key: `ui-${Date.now()}` }) }) as AutonomyRun; setAutonomy(created); setMessage("自治运行已创建，使用“推进一场”开始写入正式历史。"); } catch (reason) { setError(reason instanceof Error ? reason.message : "无法创建自治运行。"); } finally { setBusy(false); } }
  async function advanceAutonomy() { if (!autonomy) return; setBusy(true); try { const result = await api(`/projects/${params.id}/autonomy/runs/${autonomy.id}/advance`, { method: "POST", body: JSON.stringify({ max_scenes: 1, idempotency_key: `ui-${autonomy.id}` }) }) as { run: AutonomyRun; steps: unknown[] }; setAutonomy(result.run); setMessage(`本次请求复用了 ${result.steps.length} 个步骤；运行状态已更新。`); } catch (reason) { setError(reason instanceof Error ? reason.message : "推进失败。"); } finally { setBusy(false); } }
  async function pauseAutonomy() { if (!autonomy) return; setBusy(true); try { setAutonomy(await api(`/projects/${params.id}/autonomy/runs/${autonomy.id}/pause`, { method: "POST", body: JSON.stringify({ reason: "USER_PAUSED" }) }) as AutonomyRun); setMessage("自治运行已暂停。"); } catch (reason) { setError(reason instanceof Error ? reason.message : "暂停失败。"); } finally { setBusy(false); } }
  async function resumeAutonomy() { if (!autonomy) return; setBusy(true); try { setAutonomy(await api(`/projects/${params.id}/autonomy/runs/${autonomy.id}/resume`, { method: "POST" }) as AutonomyRun); setMessage("自治运行已恢复。"); } catch (reason) { setError(reason instanceof Error ? reason.message : "恢复失败。"); } finally { setBusy(false); } }

  return <main className="stack">
    <PageHeader title="导演台" description="推演下一场的候选方案。这里不会创建正式场景，也不会直接改写世界。" action={<button onClick={dryRun}><Play size={15} /> 推演下一场</button>} />
    {error && <ErrorState message={error} />}{message && <SectionCard><p>{message}</p></SectionCard>}
    <SectionCard title="自治运行" description="规则模式不会调用真实 LLM；每次推进最多提交一场正式 Scene。" aside={<StatusBadge value={autonomy?.status || "未启动"} />}>
      {!autonomy ? <div className="row"><button onClick={startAutonomy} disabled={busy}><PlayCircle size={16} /> 创建自治运行</button><span className="muted">准备好人物和剧情线程后即可开始。</span></div> : <div className="run-control"><div><strong>{autonomy.committed_scene_count || 0} / {autonomy.scene_budget} 场</strong><small>{autonomy.stop_reason || "运行边界由正式验证控制"}</small></div><div className="row"><button onClick={advanceAutonomy} disabled={busy || autonomy.status !== "RUNNING"}><Play size={15} /> 推进一场</button>{autonomy.status === "RUNNING" ? <button className="secondary" onClick={pauseAutonomy} disabled={busy}><Pause size={15} /> 暂停</button> : autonomy.status === "PAUSED" && <button className="secondary" onClick={resumeAutonomy} disabled={busy}><RotateCcw size={15} /> 恢复</button>}</div></div>}
    </SectionCard>
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
