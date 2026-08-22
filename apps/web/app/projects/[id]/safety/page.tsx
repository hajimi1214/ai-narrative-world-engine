"use client";

import { ChangeEvent, useEffect, useRef, useState } from "react";
import { Archive, CheckCircle2, Download, FileCheck2, History, RotateCcw, ShieldCheck, Upload } from "lucide-react";
import { api, apiUrl, ApiError } from "../../../lib";
import { ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../../../../components/ui/primitives";

type SafetyStatus = { safe: boolean; snapshot_count: number; last_snapshot_at?: string | null; current_checkpoint: boolean; message: string };
type Snapshot = { id: string; snapshot_type: string; state_fingerprint: string; created_at?: string };

const dateText = (value?: string | null) => value ? new Date(value).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" }) : "尚未建立";

export default function SafetyPage({ params }: { params: { id: string } }) {
  const [status, setStatus] = useState<SafetyStatus | null>(null); const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [message, setMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const load = async () => { setError(""); try { const [safety, history] = await Promise.all([api(`/projects/${params.id}/backup/status`) as Promise<SafetyStatus>, api(`/projects/${params.id}/snapshots`) as Promise<Snapshot[]>]); setStatus(safety); setSnapshots(history || []); } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取作品安全状态"); } };
  useEffect(() => { void load(); }, [params.id]);
  const download = async () => { setBusy(true); setError(""); setMessage(""); try { const response = await fetch(`${apiUrl}/projects/${params.id}/backup/export`); if (!response.ok) throw new Error("备份导出失败"); const blob = await response.blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `小说备份-${new Date().toISOString().slice(0, 10)}.json`; anchor.click(); URL.revokeObjectURL(url); setMessage("备份已下载到本机。建议保存在云盘或移动硬盘中。"); } catch (reason) { setError(reason instanceof Error ? reason.message : "备份导出失败"); } finally { setBusy(false); } };
  const checkpoint = async () => { setBusy(true); setError(""); setMessage(""); try { await api(`/projects/${params.id}/snapshots`, { method: "POST", body: JSON.stringify({ snapshot_type: "BASELINE" }) }); setMessage("当前作品状态已记为恢复点。" ); await load(); } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法创建恢复点"); } finally { setBusy(false); } };
  const importBackup = async (event: ChangeEvent<HTMLInputElement>) => { const file = event.target.files?.[0]; event.target.value = ""; if (!file) return; if (!window.confirm("导入会创建一份新的小说副本，不会覆盖当前作品。要继续吗？")) return; setBusy(true); setError(""); setMessage(""); try { const archive = JSON.parse(await file.text()); const result = await api("/projects/import", { method: "POST", body: JSON.stringify({ archive, name: `${archive.project_name || "导入的小说"}（恢复副本）` }) }) as any; setMessage("恢复副本已创建，正在打开它。" ); window.location.href = `/projects/${result.project.id}`; } catch (reason) { setError(reason instanceof ApiError ? reason.message : "导入失败，请确认选择的是平台备份文件"); } finally { setBusy(false); } };
  if (!status && !error) return <LoadingState />;
  return <main className="stack safety-page"><PageHeader title="小说安全" description="把作品保存成可带走的副本，导入时也不会覆盖当前小说。" action={<button className="secondary" onClick={() => void load()} disabled={busy}><RotateCcw size={15} />刷新状态</button>} />
    {error && <ErrorState message={error} retry={() => void load()} />}{message && <div className="success-state"><CheckCircle2 size={16} />{message}</div>}
    <section className="safety-hero"><div className="safety-hero-icon"><ShieldCheck size={25} /></div><div><p className="eyebrow">LOCAL & PRIVATE</p><h2>你的小说留在本机</h2><p>{status?.message || "导出的备份不包含模型密钥。"}</p></div><StatusBadge value={status?.safe ? "安全" : "需要检查"} /></section>
    <section className="safety-actions"><SectionCard title="保存一份备份" description="包含整本规划、人物、世界设定、研究资料、章节正文和写作规则。"><div className="safety-action-row"><div><strong>下载项目备份</strong><p>生成一个可以放进云盘、移动硬盘或另一台电脑的小说文件。</p></div><button onClick={download} disabled={busy}><Download size={16} />下载备份</button></div></SectionCard><SectionCard title="从备份恢复" description="会创建新的小说副本，当前作品不会被替换。"><div className="safety-action-row"><div><strong>导入小说备份</strong><p>选择之前下载的 `.json` 备份文件，恢复后可以继续写作。</p></div><input ref={inputRef} type="file" accept=".json,application/json" hidden onChange={importBackup} /><button className="secondary" onClick={() => inputRef.current?.click()} disabled={busy}><Upload size={16} />选择备份文件</button></div></SectionCard></section>
    <SectionCard title="恢复点" description="恢复点记录某个时刻的作品状态，用于查看和保护历史。"><div className="checkpoint-toolbar"><div><strong>{status?.snapshot_count || 0}</strong><span> 个恢复点</span><small>最近一次：{dateText(status?.last_snapshot_at)}</small></div><button className="secondary" onClick={checkpoint} disabled={busy}><FileCheck2 size={16} />记录当前状态</button></div>{snapshots.length ? <div className="snapshot-list">{snapshots.slice(0, 8).map((snapshot) => <div key={snapshot.id}><History size={15} /><div><strong>{snapshot.snapshot_type === "BASELINE" ? "作品恢复点" : "历史状态"}</strong><small>{dateText(snapshot.created_at)}</small></div><StatusBadge value="已保存" /></div>)}</div> : <p className="muted safety-empty">还没有恢复点。下载备份时也会保留当前作品内容。</p>}</SectionCard>
    <section className="safety-note"><Archive size={18} /><div><strong>小建议</strong><p>每完成一卷、一次大改或一次满意的章节质量检查，就下载一次备份。恢复副本会出现在小说库中，原稿始终保留。</p></div></section>
  </main>;
}
