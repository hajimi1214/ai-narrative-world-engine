"use client";

import { useEffect, useId, useState } from "react";
import { ArrowRight, BookOpen, Check, ChevronLeft, ChevronRight, CircleCheck, FileText, GitBranch, Radio, ShieldCheck, Sparkles, Users } from "lucide-react";
import { api } from "../../app/lib";
import { directorHref, recoveryLabel, recoveryState, type RunSummary } from "../../app/run-recovery";
import { createLivePoller } from "../live/live-poller";

type Project = { id: string; name: string };

const scenes = [
  { label: "故事规划", icon: GitBranch, title: "每一章，都有前进的方向。", subtitle: "把灵感展开成卷、故事弧与章节任务。", paper: "第一卷 · 风起时", lines: ["主线目标", "人物的选择与代价", "留给下一章的回响"], note: "作者定方向，AI 补全路径" },
  { label: "人物行动", icon: Users, title: "让人物，带着自己的记忆行动。", subtitle: "守住人物认知，让每一次选择有据可循。", paper: "角色 · 林舟", lines: ["他知道什么", "此刻想得到什么", "愿意为选择付出什么"], note: "认知、关系与经历持续传递" },
  { label: "正文与审核", icon: FileText, title: "把故事，写成可以反复打磨的正文。", subtitle: "生成、改稿、审核，保留每一个创作版本。", paper: "第一章 · 回声", lines: ["林舟合上档案。", "纸页间传来一声轻响。", "他停下，等那声音再来一次。"], note: "新稿保留历史，审核绑定版本" },
];

export function CreationShowcase({ projects, selectedProjectId, selectedRunId, selectedRunMode }: { projects: Project[]; selectedProjectId?: string; selectedRunId?: string; selectedRunMode?: string }) {
  const [index, setIndex] = useState(0);
  const [hovered, setHovered] = useState(false);
  const [run, setRun] = useState<RunSummary | null>(null);
  const [sync, setSync] = useState("等待同步");
  const [lastSync, setLastSync] = useState("");
  const selectedProject = projects.find((project) => project.id === selectedProjectId) || projects[0];
  const projectId = selectedProject?.id;
  const showcaseId = useId();
  useEffect(() => {
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const advance = () => { if (!document.hidden && !hovered && document.documentElement.dataset.motion !== "paused") setIndex((value) => (value + 1) % scenes.length); if (!cancelled) timer = setTimeout(advance, 5200); };
    timer = setTimeout(advance, 5200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [hovered]);
  useEffect(() => {
    setRun(null);
    setLastSync("");
    if (!projectId) { setSync("等待第一部作品"); return; }
    let cancelled = false;
    const poller = createLivePoller({
      intervalMs: 4000,
      onState: (state) => { if (!cancelled) { setSync(state.status === "offline" ? "连接暂断，正在重试" : state.status === "syncing" ? "正在同步" : state.status === "paused" ? "后台暂停同步" : "已连接本地工作区"); if (state.lastUpdatedAt) setLastSync(new Date(state.lastUpdatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })); } },
      load: async (signal) => {
        const result = selectedRunId
          ? await api(`/projects/${projectId}/${selectedRunMode === "AUTHOR_GUIDED_VOLUME" ? "author-guided-volume" : "auto-director"}/runs/${selectedRunId}`, { signal }) as RunSummary
          : await api(`/projects/${projectId}/auto-director/runs?limit=20&step_limit=1`, { signal }) as RunSummary[];
        if (!cancelled && !signal.aborted) setRun(Array.isArray(result) ? result.find((item) => ["CREATED", "RUNNING", "PAUSED", "FAILED", "BLOCKED"].includes(item.status || "")) || result[0] || null : result);
      },
    });
    const visibility = () => poller.setPaused(document.hidden);
    visibility(); document.addEventListener("visibilitychange", visibility);
    return () => { cancelled = true; poller.dispose(); document.removeEventListener("visibilitychange", visibility); };
  }, [projectId, selectedRunId, selectedRunMode]);
  const scene = scenes[index]; const Icon = scene.icon;
  const state = run && recoveryState(run);
  const running = state === "RUNNING" || state === "QUEUED";
  const runText = recoveryLabel(run);
  return <div className="creation-showcase" onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)} onFocusCapture={() => setHovered(true)} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setHovered(false); }} aria-roledescription="轮播图" aria-label="长篇创作流程预览">
    <div className="showcase-float float-outline" aria-hidden="true"><span><GitBranch size={14} />故事脉络</span><svg viewBox="0 0 190 62" fill="none"><path d="M5 49L43 35L77 39L114 17L148 24L183 7" stroke="#2463eb" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M5 59H185" stroke="#e1e9f6"/><circle cx="114" cy="17" r="4" fill="#2463eb"/></svg><small>让每一次转折都有来处</small></div>
    <div className="showcase-float float-library"><span><BookOpen size={14} />你的创作空间</span><strong>{projects.length.toString().padStart(2, "0")}<small>部作品</small></strong><div className="mini-bars" aria-hidden="true">{[30, 46, 37, 65, 52, 82, 68].map((height, i) => <i key={i} style={{ height }} />)}</div></div>
    <div className="showcase-window"><div className="showcase-window-bar"><div aria-hidden="true"><i/><i/><i/></div><span>NARRATIVE WORLD ENGINE</span><small><ShieldCheck size={12} />本地保存</small></div>
      <div className="showcase-stage" key={index} role="tabpanel" id={`${showcaseId}-panel`} aria-labelledby={`${showcaseId}-tab-${index}`}><div className="showcase-stage-info"><span className="showcase-kicker"><Icon size={15} />{scene.label}</span><h2>{scene.title}</h2><p>{scene.subtitle}</p><span className="showcase-assurance"><CircleCheck size={15}/>{scene.note}</span></div><div className={`showcase-paper paper-${index}`} aria-label="功能演示内容"><div className="paper-eyebrow">创作流程演示 · {String(index + 1).padStart(2, "0")}</div><h3>{scene.paper}</h3><div className="paper-divider"/>{scene.lines.map((line, i) => <p key={line} style={{ animationDelay: `${i * 110}ms` }}>{index < 2 && <Check size={14}/>} {line}</p>)}<div className="paper-ink" aria-hidden="true"/><span className="paper-page">{String(index + 1).padStart(2, "0")} / 03</span></div></div>
      <div className="showcase-tabs" role="tablist" aria-label="创作阶段">{scenes.map((item, i) => <button type="button" role="tab" id={`${showcaseId}-tab-${i}`} aria-controls={`${showcaseId}-panel`} aria-selected={i === index} tabIndex={i === index ? 0 : -1} key={item.label} onClick={() => setIndex(i)} onKeyDown={(event) => { if (["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? scenes.length - 1 : (i + (event.key === "ArrowRight" ? 1 : -1) + scenes.length) % scenes.length; setIndex(next); document.getElementById(`${showcaseId}-tab-${next}`)?.focus(); } }}><item.icon size={15}/><span>{item.label}</span><div className={i === index ? "showcase-tab-progress" : ""}/></button>)}<div className="showcase-arrows"><button type="button" aria-label="上一个创作阶段" onClick={() => setIndex((index + scenes.length - 1) % scenes.length)}><ChevronLeft size={16}/></button><button type="button" aria-label="下一个创作阶段" onClick={() => setIndex((index + 1) % scenes.length)}><ChevronRight size={16}/></button></div></div>
    </div>
    <div className="showcase-float float-live"><div><Radio size={15} className={running ? "live-radio-active" : ""}/><span>创作实况</span><b className={running ? "live-dot active" : "live-dot"}/></div><strong>{runText}</strong><p>{selectedProject?.name || "从一句灵感开始你的第一部小说"}</p><small title={lastSync ? `最近同步 ${lastSync}` : sync}>{sync}{lastSync && ` · ${lastSync}`}</small>{selectedProject && <a href={run ? directorHref(selectedProject.id, run) : `/projects/${selectedProject.id}/volumes`}>进入工作台<ArrowRight size={13}/></a>}</div>
    <span className="showcase-caption"><Sparkles size={13}/>流程预览自动轮播 · 创作实况同步本地任务</span>
  </div>;
}
