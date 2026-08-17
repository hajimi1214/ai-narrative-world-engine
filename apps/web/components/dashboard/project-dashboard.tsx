"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Clock3, GitBranch, MapPin, Sparkles, Users } from "lucide-react";
import { api } from "../../app/lib";
import { displayStatus } from "../../app/terminology";
import { EmptyState, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../ui/primitives";

const stats = [
  { key:"active_characters", title:"活跃人物", icon:Users, note:"可参与世界的角色", href:"characters" },
  { key:"canon", title:"世界事实", icon:BookOpen, note:"构成世界真相的设定", href:"canon" },
  { key:"active_story_threads", title:"剧情线程", icon:GitBranch, note:"仍在推进的冲突与目标", href:"story-threads" },
  { key:"recent_scenes", title:"最近场景", icon:Clock3, note:"已记录的世界事件", href:"director" },
];

export function ProjectDashboard({ projectId }: { projectId: string }) {
  const query = useQuery({ queryKey:["snapshot",projectId], queryFn:() => api(`/projects/${projectId}/snapshot`) as Promise<any> });
  if (query.isLoading) return <DashboardSkeleton />;
  if (query.isError) return <ErrorState message="暂时无法读取世界状态。" retry={() => void query.refetch()} />;
  const data = query.data; const project = data.project || {}; const scenes = data.recent_scenes || []; const arc = data.current_story_arc;
  const emptyWorld = (data.active_characters || []).length === 0 && scenes.length === 0 && !arc;
  return <main className="stack">
    <PageHeader title={project.name || "未命名世界"} description={`世界状态：${displayStatus(project.status)}`} action={<StatusBadge value={displayStatus(project.status)} />} />
    {emptyWorld ? <EmptyWorld projectId={projectId} data={data} /> : <RunningWorld projectId={projectId} data={data} scenes={scenes} arc={arc} />}
  </main>;
}

function StatGrid({ projectId, data }: { projectId:string; data:any }) { return <section className="stat-grid">{stats.map(stat => { const Icon = stat.icon; const count = (data[stat.key] || []).length; return <Link className="stat-card" href={`/projects/${projectId}/${stat.href}`} key={stat.key}><div className="stat-top"><span>{stat.title}</span><Icon size={17} /></div><strong>{count}</strong><small>{stat.note}</small></Link>; })}</section>; }
function EmptyWorld({ projectId, data }: { projectId:string; data:any }) { const setup = [{label:"人物", value:(data.active_characters || []).length, href:"characters", action:"创建人物"}, {label:"世界事实", value:(data.canon || []).length, href:"canon", action:"建立世界事实"}, {label:"剧情线程", value:(data.active_story_threads || []).length, href:"story-threads", action:"创建剧情线程"}]; return <><section className="world-empty-hero"><Sparkles size={26} /><div><h2>这个世界还没有开始运转。</h2><p>先补齐人物、世界事实和剧情线程，再进入导演台观察世界下一步会发生什么。</p></div></section><section className="setup-grid">{setup.map(item => <Link href={`/projects/${projectId}/${item.href}`} key={item.label}><small>{item.label}</small><strong>{item.value ? "已准备" : "待建立"}</strong><span>{item.action}</span></Link>)}<Link className="setup-director" href={`/projects/${projectId}/director`}><Sparkles size={18} />进入导演台</Link></section><StatGrid projectId={projectId} data={data} /></>; }
function RunningWorld({ projectId, data, scenes, arc }: { projectId:string; data:any; scenes:any[]; arc:any }) { return <><StatGrid projectId={projectId} data={data} /><div className="dashboard-grid"><SectionCard title="世界正在发生什么">{scenes.length ? <div className="activity-list">{scenes.map(scene => <div className="activity-item" key={scene.id}><time>{scene.world_time || `场景 ${scene.sequence}`}</time><div><strong>{scene.summary || scene.intent || "世界记录了一次事件"}</strong>{scene.location && <p className="muted"><MapPin size={13} /> {typeof scene.location === "string" ? scene.location : scene.location.name || "地点已记录"}</p>}{Array.isArray(scene.participants) && <small className="muted">参与人物：{scene.participants.length} 位</small>}</div></div>)}</div> : <EmptyState title="这个世界还没有发生场景。" description="导演提案与排演会先在临时工作区推进，正式场景将在后续流程中形成。" action={<Link className="button" href={`/projects/${projectId}/director`}>进入导演台</Link>} />}</SectionCard><aside className="stack"><SectionCard title="故事阶段">{arc ? <div className="arc-summary"><strong>{arc.title || arc.name || "未命名故事阶段"}</strong><StatusBadge value={displayStatus(arc.status)} /><p>{arc.core_question || arc.core_conflict || "核心问题尚未定义"}</p><small>当前目标：{arc.current_goal || arc.goal || "尚未设定"}</small></div> : <div className="compact-empty"><strong>当前还没有故事阶段。</strong><p>故事阶段会随着剧情结构逐渐形成。</p></div>}</SectionCard><SectionCard title="恢复中心" action={<Link className="button secondary" href={`/projects/${projectId}/recovery-candidates`}>查看恢复中心</Link>}><p className="recovery-note">候选结果不会直接修改正式世界；验证后仍需用户显式采用。</p></SectionCard></aside></div></>; }
function DashboardSkeleton() { return <main className="stack"><div className="skeleton-line skeleton-title" /><section className="stat-grid">{[1,2,3,4].map(i => <div className="skeleton-card" key={i} />)}</section><div className="dashboard-grid"><div className="skeleton-card tall" /><div className="skeleton-card tall" /></div></main>; }
