"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, BookOpen, CheckCircle2, CircleAlert, Clock3, FileText, Gauge, GitBranch, ListChecks, MapPin, PlayCircle, Sparkles, Users, WandSparkles } from "lucide-react";
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
  const query = useQuery({ queryKey:["project-control-room",projectId], queryFn:async () => {
    const snapshot = await api(`/projects/${projectId}/snapshot`) as any;
    const [plan, evaluation, next] = await Promise.all([
      api(`/projects/${projectId}/planning/plan`).catch(() => null),
      api(`/projects/${projectId}/long-form/evaluation`).catch(() => null),
      api(`/projects/${projectId}/long-form/next`).catch(() => null),
    ]);
    return { ...snapshot, plan: plan?.plan === null ? null : plan, evaluation, next };
  } });
  if (query.isLoading) return <DashboardSkeleton />;
  if (query.isError) return <ErrorState message="暂时无法读取世界状态。" retry={() => void query.refetch()} />;
  const data = query.data; const project = data.project || {}; const scenes = data.recent_scenes || []; const arc = data.current_story_arc;
  const emptyWorld = (data.active_characters || []).length === 0 && scenes.length === 0 && !arc;
  return <main className="creative-hub">
    <header className="hub-header"><div><p className="eyebrow">STORY CONTROL ROOM</p><h1>{project.name || "未命名世界"}</h1><p>{project.story_seed || "从世界、人物与一条未解的线索开始。"}</p></div><div className="hub-header-side"><StatusBadge value={displayStatus(project.status)} /><span>世界时间：{project.current_world_time || "尚未建立"}</span></div></header>
    <WorkflowOverview projectId={projectId} data={data} />
    {emptyWorld ? <EmptyWorld projectId={projectId} data={data} /> : <RunningWorld projectId={projectId} data={data} scenes={scenes} arc={arc} />}
  </main>;
}

function WorkflowOverview({ projectId, data }: { projectId: string; data: any }) {
  const plan = data.plan; const evaluation = data.evaluation; const next = data.next;
  const planned = evaluation?.summary?.planned || plan?.counts?.chapters || 0;
  const started = evaluation?.summary?.started || 0;
  const approved = plan?.status === "APPROVED";
  const chaptersProgress = planned ? Math.min(100, Math.round((started / planned) * 100)) : 0;
  const steps = [
    { label: "整本规划", detail: plan ? (approved ? "已审批" : "可审阅") : "尚未建立", done: approved, href: "planning", icon: FileText },
    { label: "人物与世界", detail: `${(data.active_characters || []).length} 人物 · ${(data.canon || []).length} 条事实`, done: (data.active_characters || []).length > 0 && (data.canon || []).length > 0, href: "characters", icon: Users },
    { label: "章节任务", detail: planned ? `${started} / ${planned} 章已开始` : "等待整本规划", done: started > 0, href: "chapters", icon: ListChecks },
    { label: "正文质量", detail: evaluation?.summary?.quality_pass_rate ? `${Math.round(evaluation.summary.quality_pass_rate * 100)}% 通过` : "尚未检查", done: Boolean(evaluation?.summary?.quality_pass_rate === 1), href: "chapters", icon: Gauge },
  ];
  const nextLabel = next?.task?.number ? `继续第 ${next.task.number} 章：${next.task.title || "打开任务单"}` : plan ? "检查并锁定下一章任务" : "先生成整本规划";
  const nextHref = `/projects/${projectId}/${next?.task?.number ? "chapters" : plan ? "chapters" : "planning"}`;
  return <section className="control-overview"><div className="control-overview-head"><div><p className="eyebrow">CREATIVE CHECKPOINT</p><h2>你的长篇进度</h2><p>从整本承诺到章节正文，每一步都能回到具体任务。</p></div><div className="control-meter"><strong>{chaptersProgress}%</strong><span>正文推进</span><div className="meter-track"><i style={{ width: `${chaptersProgress}%` }} /></div></div></div><div className="workflow-steps">{steps.map(({ label, detail, done, href, icon: Icon }, index) => <Link href={`/projects/${projectId}/${href}`} className={`workflow-step ${done ? "done" : ""}`} key={label}><span className="workflow-index">{done ? <CheckCircle2 size={16} /> : String(index + 1).padStart(2, "0")}</span><Icon size={16} /><div><strong>{label}</strong><small>{detail}</small></div><ArrowRight size={14} /></Link>)}</div><Link className="control-next" href={nextHref}><span><CircleAlert size={16} /><b>下一步</b><em>{nextLabel}</em></span><ArrowRight size={17} /></Link></section>;
}

function StatGrid({ projectId, data }: { projectId:string; data:any }) { return <section className="stat-grid">{stats.map(stat => { const Icon = stat.icon; const count = (data[stat.key] || []).length; return <Link className="stat-card" href={`/projects/${projectId}/${stat.href}`} key={stat.key}><div className="stat-top"><span>{stat.title}</span><Icon size={17} /></div><strong>{count}</strong><small>{stat.note}</small></Link>; })}</section>; }
function EmptyWorld({ projectId, data }: { projectId:string; data:any }) { const setup = [{label:"整本规划", value:0, href:"planning", action:"先定义故事方向"}, {label:"人物", value:(data.active_characters || []).length, href:"characters", action:"建立行动者"}, {label:"世界真相", value:(data.canon || []).length, href:"canon", action:"设定 Canon"}, {label:"剧情压力", value:(data.active_story_threads || []).length, href:"story-threads", action:"创建线程"}]; return <><section className="hub-blank"><div className="hub-blank-mark"><Sparkles size={28} /></div><div><p className="eyebrow">START WITH A PLAN</p><h2>先规划整本书，再进入正文。</h2><p>从灵感、篇幅、视角和文风边界开始，系统会生成可审阅的卷、故事弧和章节任务单。</p></div></section><section className="world-setup-path">{setup.map((item, index) => <Link href={`/projects/${projectId}/${item.href}`} key={item.label}><span className="path-number">0{index + 1}</span><div><small>{item.label}</small><strong>{item.value ? "已准备" : item.action}</strong></div><ArrowRight size={16} /></Link>)}<Link className="path-run" href={`/projects/${projectId}/planning`}><WandSparkles size={18} /><span>打开整本规划工作台</span></Link></section><StatGrid projectId={projectId} data={data} /></>; }
function RunningWorld({ projectId, data, scenes, arc }: { projectId:string; data:any; scenes:any[]; arc:any }) { const characters = data.active_characters || []; const threads = data.active_story_threads || []; return <><section className="hub-runway"><div><p className="eyebrow">NEXT MOVE</p><h2>{scenes.length ? "故事正在推进。下一场由谁做决定？" : "所有要素都已就位，可以开始第一场。"}</h2><p>导演只提出候选；人物、世界和验证边界决定什么能够进入正式历史。</p></div><div className="hub-runway-actions"><Link className="button" href={`/projects/${projectId}/director?mode=autonomy`}><PlayCircle size={16} />运行一场</Link><Link className="text-action" href={`/projects/${projectId}/director`}>审阅导演提案 <ArrowRight size={15} /></Link></div></section><StatGrid projectId={projectId} data={data} /><section className="hub-workspace"><div className="hub-primary"><SectionCard title="当前故事" description="最近进入正式历史的场景。" action={<Link className="text-action" href={`/projects/${projectId}/director`}>导演台 <ArrowRight size={14} /></Link>}>{scenes.length ? <div className="scene-timeline">{scenes.map(scene => <article key={scene.id}><span className="scene-sequence">{String(scene.sequence).padStart(2, "0")}</span><div><strong>{scene.summary || scene.intent || "世界记录了一次事件"}</strong><p>{scene.location ? <><MapPin size={13} /> {typeof scene.location === "string" ? scene.location : scene.location.name || "地点已记录"}</> : "地点未记录"}</p></div><small>{Array.isArray(scene.participants) ? `${scene.participants.length} 位人物` : ""}</small></article>)}</div> : <EmptyState title="尚未有正式场景" description="从导演台开始推演，系统会在验证后形成正式历史。" action={<Link className="button" href={`/projects/${projectId}/director`}>开始推演</Link>} />}</SectionCard><SectionCard title="剧情压力" description="仍在等待角色回应的线程。" action={<Link className="text-action" href={`/projects/${projectId}/story-threads`}>查看全部 <ArrowRight size={14} /></Link>}>{threads.length ? <div className="thread-list">{threads.slice(0, 4).map((thread:any) => <div key={thread.id}><span><GitBranch size={15} /></span><div><strong>{thread.title}</strong><small>{thread.goal || thread.type || "尚未描述目标"}</small></div><b>{Math.round((thread.progress || 0) * 100)}%</b></div>)}</div> : <div className="compact-empty"><strong>还没有开放剧情线程。</strong><p>线程定义了角色需要持续回应的目标、冲突和代价。</p></div>}</SectionCard></div><aside className="hub-aside"><SectionCard title="在场人物" action={<Link className="text-action" href={`/projects/${projectId}/characters`}>人物库 <ArrowRight size={14} /></Link>}><div className="cast-list">{characters.slice(0, 5).map((character:any) => <Link href={`/projects/${projectId}/characters/${character.id}`} key={character.id}><span>{character.name?.slice(0, 1) || "人"}</span><div><strong>{character.name}</strong><small>{character.goals?.current || character.goals?.primary || "正在等待目标"}</small></div></Link>)}{!characters.length && <p className="muted">先创建人物，才能生成角色行为。</p>}</div></SectionCard><SectionCard title="创作产出"><div className="output-links"><Link href={`/projects/${projectId}/chapters`}><BookOpen size={16} /><span>章节与正文投影</span><ArrowRight size={14} /></Link><Link href={`/projects/${projectId}/writing-bibles`}><CheckCircle2 size={16} /><span>写作规则与质量门禁</span><ArrowRight size={14} /></Link><Link href={`/projects/${projectId}/retcon`}><Clock3 size={16} /><span>历史改写与回放</span><ArrowRight size={14} /></Link></div></SectionCard>{arc && <SectionCard title="当前故事阶段"><div className="arc-summary"><strong>{arc.title || arc.name || "未命名故事阶段"}</strong><StatusBadge value={displayStatus(arc.status)} /><p>{arc.core_question || arc.core_conflict || "核心问题尚未定义"}</p></div></SectionCard>}</aside></section></>; }
function DashboardSkeleton() { return <main className="stack"><div className="skeleton-line skeleton-title" /><section className="stat-grid">{[1,2,3,4].map(i => <div className="skeleton-card" key={i} />)}</section><div className="dashboard-grid"><div className="skeleton-card tall" /><div className="skeleton-card tall" /></div></main>; }
