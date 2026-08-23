"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { Archive, BookOpen, BrainCircuit, ChevronLeft, ChevronRight, CircleDot, Compass, FileText, Landmark, Menu, PlayCircle, Save, Settings, ShieldAlert, Sparkles, Users, Workflow, Wrench, History, PenLine } from "lucide-react";
import { api } from "../../app/lib";
import { displayStatus } from "../../app/terminology";
import { AiLivePanel } from "../live/ai-live-panel";

type NavItem = readonly [string, string, ComponentType<{ size?: number }>, boolean?];
type NavGroup = { label: string; items: readonly NavItem[] };

const groups: readonly NavGroup[] = [
  { label: "世界", items: [["世界总览", "", Compass], ["世界实体", "world-entities", Landmark], ["世界事实", "canon", BookOpen]] },
  { label: "人物", items: [["人物档案", "characters", Users]] },
  { label: "剧情", items: [["自动导演", "director/auto", Sparkles], ["整本规划", "planning", BookOpen], ["卷总览", "volumes", BookOpen], ["跨卷连续性", "continuity", History], ["当前卷导演", "director/volume", Sparkles], ["剧情线程", "story-threads", Workflow], ["导演台", "director", Sparkles], ["世界运行", "world-run", PlayCircle]] },
  { label: "写作", items: [["章节工作台", "chapters", PenLine], ["写作手册", "writing-bibles", FileText], ["反 AI 规则", "anti-ai-bibles", ShieldAlert]] },
  { label: "作品安全", items: [["小说安全", "safety", Archive], ["异常修复", "recovery-candidates", Wrench], ["历史改写", "retcon", History]] },
  { label: "设置", items: [["模型配置", "model-config", Settings]] },
] as const;

export function ProjectShell({ projectId, children }: { projectId: string; children: ReactNode }) {
  const pathname = usePathname(); const [collapsed, setCollapsed] = useState(false); const [drawer, setDrawer] = useState(false); const [project, setProject] = useState<any>(); const [savedAt, setSavedAt] = useState<number | null>(null);
  useEffect(() => { setCollapsed(localStorage.getItem("nwe-sidebar-collapsed") === "true"); api(`/projects/${projectId}`).then(setProject).catch(() => undefined); }, [projectId]);
  const draftKey = () => `nwe-page-draft:${projectId}:${pathname}`;
  const editableFields = () => Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(".main-content input, .main-content textarea, .main-content select")).filter((field) => {
    const inputType = field instanceof HTMLInputElement ? field.type : "";
    return !field.disabled && !field.closest("[data-no-page-draft]") && !["password", "file", "hidden", "submit", "button", "reset"].includes(inputType);
  });
  const savePageDraft = () => {
    const fields = editableFields();
    const values = fields.map((field, index) => ({
      key: `${index}:${field.tagName}:${field instanceof HTMLInputElement ? field.type : "value"}`,
      value: field.value,
      checked: field instanceof HTMLInputElement ? field.checked : undefined,
    }));
    const draft = { version: 1, saved_at: new Date().toISOString(), values };
    if (values.length) {
      localStorage.setItem(draftKey(), JSON.stringify(draft));
      const autonomySettings = { ...(project?.autonomy_settings || {}), page_drafts: { ...(project?.autonomy_settings?.page_drafts || {}), [pathname]: draft } };
      void api(`/projects/${projectId}`, { method: "PATCH", body: JSON.stringify({ autonomy_settings: autonomySettings }) }).then(setProject).catch(() => undefined);
    }
    setSavedAt(Date.now());
  };
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const raw = localStorage.getItem(draftKey()) || (project?.autonomy_settings?.page_drafts?.[pathname] ? JSON.stringify(project.autonomy_settings.page_drafts[pathname]) : null);
      if (!raw) return;
      try {
        const draft = JSON.parse(raw) as { values?: Array<{ key: string; value: string; checked?: boolean }> };
        const saved = new Map((draft.values || []).map((item) => [item.key, item]));
        editableFields().forEach((field, index) => {
          const item = saved.get(`${index}:${field.tagName}:${field instanceof HTMLInputElement ? field.type : "value"}`);
          if (!item) return;
          if (field instanceof HTMLInputElement && ["checkbox", "radio"].includes(field.type)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked")?.set;
            setter?.call(field, Boolean(item.checked));
          } else {
            const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : field instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(field, item.value);
          }
          field.dispatchEvent(new Event("input", { bubbles: true }));
          field.dispatchEvent(new Event("change", { bubbles: true }));
        });
      } catch { localStorage.removeItem(draftKey()); }
    }, 100);
    const saveBeforeExit = () => savePageDraft();
    window.addEventListener("beforeunload", saveBeforeExit);
    return () => { window.clearTimeout(timer); window.removeEventListener("beforeunload", saveBeforeExit); };
  }, [pathname, projectId, project]);
  function toggle() { const next = !collapsed; setCollapsed(next); localStorage.setItem("nwe-sidebar-collapsed", String(next)); }
  const nav = <nav className="sidebar-nav">{groups.map((group) => <div className="nav-group" key={group.label}><span className="nav-label">{group.label}</span>{group.items.map(([name, route, Icon, disabled]) => { const href = `/projects/${projectId}${route ? `/${route}` : ""}`; const active = pathname === href; return disabled ? <span className="nav-item disabled" title="规划中" key={route}><Icon size={18} /><b>{name}</b></span> : <Link className={`nav-item ${active ? "active" : ""}`} href={href} key={route} aria-current={active ? "page" : undefined} onClick={() => { savePageDraft(); setDrawer(false); }}><Icon size={18} /><b>{name}</b></Link>; })}</div>)}</nav>;
  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}><aside className={`sidebar ${drawer ? "drawer-open" : ""}`}><div className="brand-lockup"><BrainCircuit size={24} /><div><strong>AI 自演小说世界引擎</strong><small>Narrative World Engine</small></div></div>{nav}<button className="collapse-button" title={collapsed ? "展开侧边栏" : "收起侧边栏"} onClick={toggle} aria-label="收起侧边栏">{collapsed ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}</button></aside>{drawer && <button className="drawer-backdrop" aria-label="关闭菜单" onClick={() => setDrawer(false)} /> }<div className="work-area"><header className="workspace-topbar"><button className="mobile-menu" aria-label="打开导航" onClick={() => setDrawer(true)}><Menu size={20} /></button><div className="breadcrumb"><Link href="/">项目</Link><span>/</span><strong>{project?.name || "加载项目…"}</strong></div><div className="workspace-topbar-actions"><div className="world-status"><span><CircleDot size={14} /> {displayStatus(project?.status)}</span><span>世界时间：{project?.current_world_time || "尚未建立世界时间"}</span></div><button className="secondary page-draft-save" onClick={savePageDraft} title="将本页尚未提交的表单保存到本地和当前项目"><Save size={15} />{savedAt ? "已保存" : "保存此页"}</button><AiLivePanel projectId={projectId} /></div></header><main className="main-content">{children}</main></div></div>;
}
