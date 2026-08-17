"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "../../../lib";

export default function RecoveryCandidatesPage({ params }: { params: { id: string } }) {
  const [items, setItems] = useState<any[]>([]);
  const [error, setError] = useState<string>();
  useEffect(() => { api(`/projects/${params.id}/recovery-candidates`).then(setItems).catch((e) => setError(e.message)); }, [params.id]);
  return <main className="stack"><section><h1>Recovery Candidates</h1><p className="muted">这里只是在修改候选结果，不会直接修改正式世界。</p></section>{error && <p>{error}</p>}<section className="stack">{items.map((item) => <article className="panel" key={item.id}><h2>{item.candidate_type} · {item.status}</h2><p>Initial error: {item.initial_error_code || "-"}</p><p>Version: {item.current_version_number} · Trace: {item.source_trace_id}</p><p>Created: {item.created_at}</p><p>Actions: {(item.available_actions || []).join(", ") || "-"}</p><Link href={`/projects/${params.id}/recovery-candidates/${item.id}`}>查看详情</Link></article>)}</section></main>;
}
