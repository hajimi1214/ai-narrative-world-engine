"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { Archive, BookOpen, BrainCircuit, ChevronLeft, ChevronRight, CircleDot, Compass, FileText, Landmark, Menu, PlayCircle, Settings, ShieldAlert, Sparkles, Users, Workflow, Wrench, History, PenLine } from "lucide-react";
import { api } from "../../app/lib";
import { displayStatus } from "../../app/terminology";
import { AiLivePanel } from "../live/ai-live-panel";

type NavItem = readonly [string, string, ComponentType<{ size?: number }>, boolean?];
type NavGroup = { label: string; items: readonly NavItem[] };

const groups: readonly NavGroup[] = [
  { label: "世界", items: [["世界总览", "", Compass], ["世界实体", "world-entities", Landmark], ["世界事实", "canon", BookOpen]] },
  { label: "人物", items: [["人物档案", "characters", Users]] },
  { label: "剧情", items: [["整本规划", "planning", BookOpen], ["剧情线程", "story-threads", Workflow], ["导演台", "director", Sparkles], ["世界运行", "director?mode=autonomy", PlayCircle]] },
  { label: "写作", items: [["章节工作台", "chapters", PenLine], ["写作手册", "writing-bibles", FileText], ["反 AI 规则", "anti-ai-bibles", ShieldAlert]] },
  { label: "作品安全", items: [["小说安全", "safety", Archive], ["异常修复", "recovery-candidates", Wrench], ["历史改写", "retcon", History]] },
  { label: "设置", items: [["模型配置", "model-config", Settings]] },
] as const;

export function ProjectShell({ projectId, children }: { projectId: string; children: ReactNode }) {
  const pathname = usePathname(); const [collapsed, setCollapsed] = useState(false); const [drawer, setDrawer] = useState(false); const [project, setProject] = useState<any>();
  useEffect(() => { setCollapsed(localStorage.getItem("nwe-sidebar-collapsed") === "true"); api(`/projects/${projectId}`).then(setProject).catch(() => undefined); }, [projectId]);
  function toggle() { const next = !collapsed; setCollapsed(next); localStorage.setItem("nwe-sidebar-collapsed", String(next)); }
  const current = pathname.split("/").filter(Boolean).slice(-1)[0];
  const nav = <nav className="sidebar-nav">{groups.map((group) => <div className="nav-group" key={group.label}><span className="nav-label">{group.label}</span>{group.items.map(([name, route, Icon, disabled]) => { const href = `/projects/${projectId}${route ? `/${route}` : ""}`; const routePath = route.split("?")[0]; const active = route === "" ? pathname === href : current === routePath; return disabled ? <span className="nav-item disabled" title="规划中" key={route}><Icon size={18} /><b>{name}</b></span> : <Link className={`nav-item ${active ? "active" : ""}`} href={href} key={route} aria-current={active ? "page" : undefined} onClick={() => setDrawer(false)}><Icon size={18} /><b>{name}</b></Link>; })}</div>)}</nav>;
  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}><aside className={`sidebar ${drawer ? "drawer-open" : ""}`}><div className="brand-lockup"><BrainCircuit size={24} /><div><strong>AI 自演小说世界引擎</strong><small>Narrative World Engine</small></div></div>{nav}<button className="collapse-button" title={collapsed ? "展开侧边栏" : "收起侧边栏"} onClick={toggle} aria-label="收起侧边栏">{collapsed ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}</button></aside>{drawer && <button className="drawer-backdrop" aria-label="关闭菜单" onClick={() => setDrawer(false)} /> }<div className="work-area"><header className="workspace-topbar"><button className="mobile-menu" aria-label="打开导航" onClick={() => setDrawer(true)}><Menu size={20} /></button><div className="breadcrumb"><Link href="/">项目</Link><span>/</span><strong>{project?.name || "加载项目…"}</strong></div><div className="workspace-topbar-actions"><div className="world-status"><span><CircleDot size={14} /> {displayStatus(project?.status)}</span><span>世界时间：{project?.current_world_time || "尚未建立世界时间"}</span></div><AiLivePanel projectId={projectId} /></div></header><main className="main-content">{children}</main></div></div>;
}
