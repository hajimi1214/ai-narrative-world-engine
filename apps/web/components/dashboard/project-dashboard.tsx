"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Clock3, GitBranch, MapPin, Users, Wrench } from "lucide-react";
import { api } from "../../app/lib";
import { EmptyState, ErrorState, LoadingState, PageHeader, SectionCard, StatusBadge } from "../ui/primitives";

const stats = [{ key:"active_characters", title:"活跃人物", icon:Users, note:"当前能够参与世界的角色" }, { key:"canon", title:"世界事实", icon:BookOpen, note:"作为世界真相的基础设定" }, { key:"active_story_threads", title:"剧情线程", icon:GitBranch, note:"尚在推进的冲突与目标" }, { key:"recent_scenes", title:"最近场景", icon:Clock3, note:"已记录的世界事件" }];
export function ProjectDashboard({ projectId }: { projectId: string }) {
  const query = useQuery({ queryKey:["snapshot",projectId], queryFn:() => api(`/projects/${projectId}/snapshot`) as Promise<any> });
  if (query.isLoading) return <LoadingState />; if (query.isError) return <ErrorState message="无法加载项目总览。" retry={() => query.refetch()} />;
  const data = query.data; const project = data.project; const arc = data.current_story_arc; const scenes = data.recent_scenes || [];
  return <><PageHeader title={project.name} description={`世界状态：${project.status === "ACTIVE" ? "正在运行" : "已暂停"}`} action={<StatusBadge value={project.status} />} /><section className="stat-grid">{stats.map(stat => { const Icon = stat.icon; const items = data[stat.key] || []; return <article className="stat-card" key={stat.key}><div className="stat-top"><span>{stat.title}</span><Icon size={18} /></div><strong>{items.length}</strong><small>{stat.note}</small></article>; })}</section><div className="dashboard-grid"><SectionCard title="世界正在发生什么">{scenes.length ? <div className="activity-list">{scenes.map((scene:any) => <div className="activity-item" key={scene.id}><time>场景 {scene.sequence}</time><div><strong>{scene.summary || scene.intent || "世界记录了一次事件"}</strong><p className="muted">{scene.location ? <><MapPin size={13} /> {scene.location}</> : "地点尚未记录"}</p></div></div>)}</div> : <EmptyState title="这个世界还没有发生场景。" description="场景会在未来的导演提案与排演流程中形成世界历史。" />}</SectionCard><div className="stack"><SectionCard title="故事阶段">{arc ? <div className="stack"><div><strong>{arc.title}</strong><p className="muted">{arc.core_question || arc.core_conflict || "核心问题尚未定义"}</p></div><StatusBadge value={arc.status} /><small className="muted">当前进度 {Math.round((arc.progress || 0) * 100)}%</small></div> : <EmptyState title="当前还没有故事阶段。" description="故事阶段会在世界结构逐步稳定后出现。" />}</SectionCard><SectionCard title="恢复中心" action={<Link className="button secondary" href={`/projects/${projectId}/recovery-candidates`}>查看恢复中心</Link>}><p className="recovery-note">候选结果不会直接修改正式世界；只有验证并由用户显式 Adopt 后，才会修复对应排演产物。</p></SectionCard></div></div></>;
}
