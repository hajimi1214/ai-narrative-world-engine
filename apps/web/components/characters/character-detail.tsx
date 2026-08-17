"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Brain, BriefcaseBusiness, HeartHandshake, Package, Sparkles, UserRound } from "lucide-react";
import { api } from "../../app/lib";
import { displayStatus, term } from "../../app/terminology";
import { DeveloperData, EmptyState, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../ui/primitives";

const tabs = [["overview", "概览", UserRound], ["knowledge", "认知", Brain], ["memory", "记忆", BookOpen], ["relationships", "关系", HeartHandshake], ["abilities", "能力", Sparkles], ["inventory", "物品", Package], ["arc", "人物弧", BriefcaseBusiness], ["developer", "开发者数据", UserRound]] as const;
const present = (value: unknown) => value === null || value === undefined || value === "" ? "-" : typeof value === "object" ? "-" : String(value);

export function CharacterDetail({ characterId }: { characterId: string }) {
  const [tab, setTab] = useState<(typeof tabs)[number][0]>("overview");
  const character = useQuery({ queryKey:["character", characterId], queryFn:() => api(`/characters/${characterId}`) as Promise<any> });
  const knowledge = useQuery({ queryKey:["knowledge", characterId], queryFn:() => api(`/characters/${characterId}/knowledge`) as Promise<any[]> });
  const memories = useQuery({ queryKey:["memories", characterId], queryFn:() => api(`/characters/${characterId}/memories`) as Promise<any[]> });
  if (character.isLoading) return <LoadingState />;
  if (character.isError) return <ErrorState message="暂时无法读取人物数据。" retry={() => void character.refetch()} />;
  const data = character.data;
  const facts = [["当前位置", data.current_state?.location_id], ["当前目标", data.goals?.current], ["当前情绪", data.emotional_state?.current || data.emotional_state?.mood], ["身体状态", data.physical_state?.status]];
  const renderList = (title:string, records:unknown, empty:string) => <SectionCard title={title}>{Array.isArray(records) && records.length ? <div className="detail-list">{records.map((item:any, index) => <div className="detail-line" key={item.id || index}><strong>{item.name || item.title || item.description || String(item)}</strong>{typeof item === "object" && <span>{present(item.status || item.cost || item.quantity)}</span>}</div>)}</div> : <EmptyState title={empty} description="随着世界演出和角色发展，这里会逐步出现记录。" />}</SectionCard>;
  return <main className="stack">
    <PageHeader title={data.name} description={data.profile?.role || data.profile?.identity || "人物档案"} action={<StatusBadge value={displayStatus(data.active === false ? "PAUSED" : "ACTIVE")} />} />
    <section className="character-hero"><div className="avatar-placeholder" aria-hidden="true">{data.name?.slice(0, 1)}</div><div className="state-grid">{facts.map(([label, value]) => <div key={label}><small>{label}</small><strong>{present(value)}</strong></div>)}</div></section>
    <nav className="detail-tabs" aria-label="人物详情导航">{tabs.map(([key, label, Icon]) => <button key={key} className={tab === key ? "active" : ""} aria-current={tab === key ? "page" : undefined} onClick={() => setTab(key)}><Icon size={15} /> {label}</button>)}</nav>
    {tab === "overview" && <SectionCard title="人物概览"><p>{data.profile?.description || data.profile?.summary || "暂无人物简介。"}</p><div className="key-value-grid">{Object.entries(data.profile || {}).filter(([,value]) => typeof value !== "object").slice(0,8).map(([key,value]) => <div key={key}><small>{key}</small><span>{String(value)}</span></div>)}</div></SectionCard>}
    {tab === "knowledge" && <SectionCard title="人物认知">{knowledge.isLoading ? <LoadingState /> : knowledge.isError ? <ErrorState message="暂时无法读取人物认知。" retry={() => void knowledge.refetch()} /> : <div className="knowledge-grid">{["KNOWN", "SUSPECTED", "FALSE_BELIEF"].map(status => <div key={status}><h3>{term(status)}</h3>{knowledge.data?.filter(item => item.status === status).length ? knowledge.data.filter(item => item.status === status).map(item => <p className="knowledge-line" key={item.id}><strong>{item.proposition}</strong><small>可信度：{present(item.confidence)} · 来源：{present(item.source)}</small></p>) : <p className="muted">暂无记录</p>}</div>)}</div>}</SectionCard>}
    {tab === "memory" && <SectionCard title="人物记忆">{memories.isLoading ? <LoadingState /> : memories.isError ? <ErrorState message="暂时无法读取人物记忆。" retry={() => void memories.refetch()} /> : memories.data?.length ? <div className="memory-list">{memories.data.map(item => <article key={item.id}><div><strong>{item.content}</strong><small>{item.happened_at || "时间未记录"}</small></div><span title={`重要度 ${item.importance || 0}`}>{"★".repeat(Math.max(0, Math.min(5, Math.round(item.importance || 0)))) || "-"}</span></article>)}</div> : <EmptyState title="暂无记忆记录" description="人物经历过可沉淀的事件后，记忆会出现在这里。" />}</SectionCard>}
    {tab === "relationships" && renderList("人物关系", Object.entries(data.relationships || {}).map(([name, value]) => ({ name, description: typeof value === "object" ? JSON.stringify(value) : value })), "暂无关系记录")}
    {tab === "abilities" && renderList("能力", data.abilities, "暂无能力记录")}
    {tab === "inventory" && renderList("物品", data.inventory, "暂无物品记录")}
    {tab === "arc" && <EmptyState title="当前还没有人物弧记录" description="人物弧会在后续剧情与正式场景中逐步沉淀。" />}
    {tab === "developer" && <DeveloperData value={data} label="人物原始数据" />}
  </main>;
}
