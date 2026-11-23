"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Brain, BriefcaseBusiness, HeartHandshake, MessageCircle, Package, Save, Sparkles, UserRound } from "lucide-react";
import { api } from "../../app/lib";
import { displayStatus, term } from "../../app/terminology";
import { DeveloperData, EmptyState, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../ui/primitives";

const tabs = [["overview", "概览", UserRound], ["voice", "表达声线", MessageCircle], ["knowledge", "认知", Brain], ["memory", "记忆", BookOpen], ["relationships", "关系", HeartHandshake], ["abilities", "能力", Sparkles], ["inventory", "物品", Package], ["arc", "人物弧", BriefcaseBusiness], ["developer", "开发者数据", UserRound]] as const;
const present = (value: unknown) => value === null || value === undefined || value === "" ? "-" : typeof value === "object" ? "-" : String(value);
const lines = (value: unknown) => Array.isArray(value) ? value.join("\n") : "";
const lineItems = (value: string) => value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
type VoiceDraft = { register: string; diction: string; sentence_length: string; rhythm: string; directness: string; metaphor_style: string; dialogue_habits: string; verbal_tics: string; avoid: string };
const emptyVoice: VoiceDraft = { register: "", diction: "", sentence_length: "", rhythm: "", directness: "", metaphor_style: "", dialogue_habits: "", verbal_tics: "", avoid: "" };

export function CharacterDetail({ characterId }: { characterId: string }) {
  const [tab, setTab] = useState<(typeof tabs)[number][0]>("overview");
  const [voiceDraft, setVoiceDraft] = useState<VoiceDraft>(emptyVoice);
  const [voiceSaving, setVoiceSaving] = useState(false);
  const [voiceMessage, setVoiceMessage] = useState("");
  const [voiceError, setVoiceError] = useState("");
  const queryClient = useQueryClient();
  const character = useQuery({ queryKey:["character", characterId], queryFn:() => api(`/characters/${characterId}`) as Promise<any> });
  const knowledge = useQuery({ queryKey:["knowledge", characterId], queryFn:() => api(`/characters/${characterId}/knowledge`) as Promise<any[]> });
  const memories = useQuery({ queryKey:["memories", characterId], queryFn:() => api(`/characters/${characterId}/memories`) as Promise<any[]> });
  const data = character.data;
  useEffect(() => {
    setVoiceMessage(""); setVoiceError("");
  }, [characterId]);
  useEffect(() => {
    if (!data) return;
    const profile = data.voice_profile || {};
    setVoiceDraft({
      register: String(profile.register || ""),
      diction: String(profile.diction || ""),
      sentence_length: String(profile.sentence_length || ""),
      rhythm: String(profile.rhythm || ""),
      directness: String(profile.directness || ""),
      metaphor_style: String(profile.metaphor_style || ""),
      dialogue_habits: lines(profile.dialogue_habits),
      verbal_tics: lines(profile.verbal_tics),
      avoid: lines(profile.avoid),
    });
  }, [data]);
  if (character.isLoading) return <LoadingState />;
  if (character.isError) return <ErrorState message="暂时无法读取人物数据。" retry={() => void character.refetch()} />;
  async function saveVoiceProfile() {
    setVoiceSaving(true); setVoiceMessage(""); setVoiceError("");
    const profile = Object.fromEntries(Object.entries({
      register: voiceDraft.register.trim(), diction: voiceDraft.diction.trim(),
      sentence_length: voiceDraft.sentence_length.trim(), rhythm: voiceDraft.rhythm.trim(),
      directness: voiceDraft.directness.trim(), metaphor_style: voiceDraft.metaphor_style.trim(),
      dialogue_habits: lineItems(voiceDraft.dialogue_habits), verbal_tics: lineItems(voiceDraft.verbal_tics),
      avoid: lineItems(voiceDraft.avoid),
    }).filter(([, value]) => Array.isArray(value) ? value.length > 0 : Boolean(value)));
    try {
      await api(`/characters/${characterId}`, { method: "PATCH", body: JSON.stringify({ voice_profile: profile }) });
      await queryClient.invalidateQueries({ queryKey: ["character", characterId] });
      setVoiceMessage("表达声线已保存。之后写作将把这些设定作为对白风格参考。");
    } catch (reason) {
      setVoiceError(reason instanceof Error ? reason.message : "保存表达声线失败。");
    } finally { setVoiceSaving(false); }
  }
  const facts = [["当前位置", data.current_state?.location_id], ["当前目标", data.goals?.current], ["当前情绪", data.emotional_state?.current || data.emotional_state?.mood], ["身体状态", data.physical_state?.status]];
  const renderList = (title:string, records:unknown, empty:string) => <SectionCard title={title}>{Array.isArray(records) && records.length ? <div className="detail-list">{records.map((item:any, index) => <div className="detail-line" key={item.id || index}><strong>{item.name || item.title || item.description || String(item)}</strong>{typeof item === "object" && <span>{present(item.status || item.cost || item.quantity)}</span>}</div>)}</div> : <EmptyState title={empty} description="随着世界演出和角色发展，这里会逐步出现记录。" />}</SectionCard>;
  return <main className="stack">
    <PageHeader title={data.name} description={data.profile?.role || data.profile?.identity || "人物档案"} action={<StatusBadge value={displayStatus(data.active === false ? "PAUSED" : "ACTIVE")} />} />
    <section className="character-hero"><div className="avatar-placeholder" aria-hidden="true">{data.name?.slice(0, 1)}</div><div className="state-grid">{facts.map(([label, value]) => <div key={label}><small>{label}</small><strong>{present(value)}</strong></div>)}</div></section>
    <nav className="detail-tabs" aria-label="人物详情导航">{tabs.map(([key, label, Icon]) => <button key={key} className={tab === key ? "active" : ""} aria-current={tab === key ? "page" : undefined} onClick={() => setTab(key)}><Icon size={15} /> {label}</button>)}</nav>
    {tab === "overview" && <SectionCard title="人物概览"><p>{data.profile?.description || data.profile?.summary || "暂无人物简介。"}</p><div className="key-value-grid">{Object.entries(data.profile || {}).filter(([,value]) => typeof value !== "object").slice(0,8).map(([key,value]) => <div key={key}><small>{key}</small><span>{String(value)}</span></div>)}</div></SectionCard>}
    {tab === "voice" && <SectionCard title="表达声线" description="只描述角色说话时的语域、节奏和措辞，不填写具体台词、剧情内容或秘密。Writer 会把这些设定作为对白风格参考，不会当成故事事实，也不会强迫角色重复口头禅。"><div className="form-grid">
      <label className="field"><span>语域与场合</span><textarea value={voiceDraft.register} onChange={(event) => setVoiceDraft({ ...voiceDraft, register: event.target.value })} placeholder="例如：对长辈客气，工作时简短清楚" /></label>
      <label className="field"><span>常用措辞</span><textarea value={voiceDraft.diction} onChange={(event) => setVoiceDraft({ ...voiceDraft, diction: event.target.value })} placeholder="例如：偏具体名词，不用网络流行语" /></label>
      <label className="field"><span>句子长度</span><input value={voiceDraft.sentence_length} onChange={(event) => setVoiceDraft({ ...voiceDraft, sentence_length: event.target.value })} placeholder="例如：平时短句，解释时会补充缘由" /></label>
      <label className="field"><span>表达节奏</span><input value={voiceDraft.rhythm} onChange={(event) => setVoiceDraft({ ...voiceDraft, rhythm: event.target.value })} placeholder="例如：先停顿，再给结论" /></label>
      <label className="field"><span>直接程度</span><input value={voiceDraft.directness} onChange={(event) => setVoiceDraft({ ...voiceDraft, directness: event.target.value })} placeholder="例如：回避冲突时绕开问题" /></label>
      <label className="field"><span>比喻习惯</span><input value={voiceDraft.metaphor_style} onChange={(event) => setVoiceDraft({ ...voiceDraft, metaphor_style: event.target.value })} placeholder="例如：只用与修复工作相关的比喻" /></label>
      <label className="field full"><span>对白习惯 <small>每项一行</small></span><textarea value={voiceDraft.dialogue_habits} onChange={(event) => setVoiceDraft({ ...voiceDraft, dialogue_habits: event.target.value })} /></label>
      <label className="field full"><span>可选口头表达 <small>每项一行，避免每句都重复</small></span><textarea value={voiceDraft.verbal_tics} onChange={(event) => setVoiceDraft({ ...voiceDraft, verbal_tics: event.target.value })} /></label>
      <label className="field full"><span>不符合该人物的表达 <small>每项一行</small></span><textarea value={voiceDraft.avoid} onChange={(event) => setVoiceDraft({ ...voiceDraft, avoid: event.target.value })} /></label>
    </div>{voiceError && <ErrorState message={voiceError} retry={() => setVoiceError("")} />}{voiceMessage && <p className="success-note" role="status">{voiceMessage}</p>}<button onClick={() => void saveVoiceProfile()} disabled={voiceSaving}><Save size={15} />{voiceSaving ? "正在保存…" : "保存表达声线"}</button></SectionCard>}
    {tab === "knowledge" && <SectionCard title="人物认知">{knowledge.isLoading ? <LoadingState /> : knowledge.isError ? <ErrorState message="暂时无法读取人物认知。" retry={() => void knowledge.refetch()} /> : <div className="knowledge-grid">{["KNOWN", "SUSPECTED", "FALSE_BELIEF"].map(status => <div key={status}><h3>{term(status)}</h3>{knowledge.data?.filter(item => item.status === status).length ? knowledge.data.filter(item => item.status === status).map(item => <p className="knowledge-line" key={item.id}><strong>{item.proposition}</strong><small>可信度：{present(item.confidence)} · 来源：{present(item.source)}</small></p>) : <p className="muted">暂无记录</p>}</div>)}</div>}</SectionCard>}
    {tab === "memory" && <SectionCard title="人物记忆">{memories.isLoading ? <LoadingState /> : memories.isError ? <ErrorState message="暂时无法读取人物记忆。" retry={() => void memories.refetch()} /> : memories.data?.length ? <div className="memory-list">{memories.data.map(item => <article key={item.id}><div><strong>{item.content}</strong><small>{item.happened_at || "时间未记录"}</small></div><span title={`重要度 ${item.importance || 0}`}>{"★".repeat(Math.max(0, Math.min(5, Math.round(item.importance || 0)))) || "-"}</span></article>)}</div> : <EmptyState title="暂无记忆记录" description="人物经历过可沉淀的事件后，记忆会出现在这里。" />}</SectionCard>}
    {tab === "relationships" && renderList("人物关系", Object.entries(data.relationships || {}).map(([name, value]) => ({ name, description: typeof value === "object" ? JSON.stringify(value) : value })), "暂无关系记录")}
    {tab === "abilities" && renderList("能力", data.abilities, "暂无能力记录")}
    {tab === "inventory" && renderList("物品", data.inventory, "暂无物品记录")}
    {tab === "arc" && <EmptyState title="当前还没有人物弧记录" description="人物弧会在后续剧情与正式场景中逐步沉淀。" />}
    {tab === "developer" && <DeveloperData value={data} label="人物原始数据" />}
  </main>;
}
