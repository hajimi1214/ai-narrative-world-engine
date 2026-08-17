"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "../../../lib";
import { term } from "../../../terminology";

type RecoveryCandidateResponse = {
  candidate: { id: string; candidate_type: string; status: string; initial_error_code?: string; current_version_number: number; source_trace_id: string; created_at: string };
  available_actions: string[];
};

export default function RecoveryCandidatesPage({ params }: { params: { id: string } }) {
  const [items, setItems] = useState<RecoveryCandidateResponse[]>([]);
  const [error, setError] = useState<string>();
  useEffect(() => { api(`/projects/${params.id}/recovery-candidates`).then(setItems).catch((e) => setError(e.message)); }, [params.id]);
  return <main className="stack"><section><h1>恢复中心</h1><p className="muted">这里只是在修改候选结果，不会直接修改正式世界。</p></section>{error && <p>{error}</p>}<section className="stack">{items.length ? items.map((item) => <article className="panel" key={item.candidate.id}><h2>{term(item.candidate.candidate_type)} · {term(item.candidate.status)}</h2><p>初始错误：{term(item.candidate.initial_error_code)}</p><p>当前版本：{item.candidate.current_version_number} · 来源追踪：{item.candidate.source_trace_id}</p><p>可执行操作：{item.available_actions.map(term).join("、") || "-"}</p><Link className="button secondary" href={`/projects/${params.id}/recovery-candidates/${item.candidate.id}`}>查看详情</Link></article>) : <article className="panel"><p>当前没有待处理的恢复候选。</p></article>}</section></main>;
}
