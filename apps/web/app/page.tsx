"use client";

import { FormEvent, useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, BookOpen, BrainCircuit, Check, CheckCircle2, Clock3, Eye, FileText, FolderOpen, Plus, Radio, Settings2, ShieldCheck, Sparkles, Upload, Users, X } from "lucide-react";
import { api, ApiError } from "./lib";
import { directorHref, type RunSummary } from "./run-recovery";
import { displayStatus } from "./terminology";
import { EmptyState, ErrorState, LoadingState, StatusBadge } from "../components/ui/primitives";
import { CreationShowcase } from "../components/home/creation-showcase";

type Project = { id: string; name: string; status: string; story_seed?: string; updated_at?: string };
type Direction = Record<string, any>;
type Mode = "inspiration" | "outline" | "prose" | "world" | "blank";
type Workflow = "author" | "auto" | "planning";
const modes: Array<{ id: Mode; title: string; description: string; icon: typeof Sparkles }> = [
  { id: "inspiration", title: "从一句灵感开始", description: "只有一个念头，让 AI 帮你长成整本方向。", icon: Sparkles },
  { id: "outline", title: "从已有大纲开始", description: "保留你的结构，再补齐卷、人物和伏笔。", icon: FileText },
  { id: "prose", title: "从已有正文继续", description: "导入已有片段，接着规划后续长篇。", icon: BookOpen },
  { id: "world", title: "导入世界观", description: "先提供世界边界，再让故事从里面生长。", icon: Upload },
  { id: "blank", title: "空白创作", description: "从类型、读者和文风开始决定方向。", icon: Plus },
];
const blankDirection = { title: "", selling_point: "", premise: "", ending: "", core_conflict: "", world_boundaries: [], protagonist: { name: "", role: "", desire: "", cost: "" }, first_volume_goal: "", first_ten_chapter_promises: [], foreshadowing_directions: [], style_advice: "" };
const lines = (value: unknown) => Array.isArray(value) ? value.join("\n") : String(value || "");

export default function Home() {
  const [projects, setProjects] = useState<Project[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const [mode, setMode] = useState<Mode>("inspiration"); const [name, setName] = useState(""); const [inspiration, setInspiration] = useState(""); const [source, setSource] = useState(""); const [genre, setGenre] = useState(""); const [audience, setAudience] = useState("成人读者"); const [chapters, setChapters] = useState(50); const [words, setWords] = useState(3000); const [pov, setPov] = useState("THIRD_PERSON_LIMITED"); const [tone, setTone] = useState("克制、具体、有画面感"); const [styleSamples, setStyleSamples] = useState(""); const [forbidden, setForbidden] = useState("");
  const [projectId, setProjectId] = useState(""); const [autoProjectId, setAutoProjectId] = useState(""); const [activeRunId, setActiveRunId] = useState(""); const [activeRunMode, setActiveRunMode] = useState(""); const [activeRunHref, setActiveRunHref] = useState(""); const [directions, setDirections] = useState<Direction[]>([]); const [selected, setSelected] = useState(0); const [busy, setBusy] = useState(false); const [step, setStep] = useState<"form" | "directions">("form");
  const [showWizard, setShowWizard] = useState(false); const [showTour, setShowTour] = useState(false); const [showModelSetup, setShowModelSetup] = useState(false); const [workflow, setWorkflow] = useState<Workflow>("author"); const [lengthMode, setLengthMode] = useState("ESTIMATE_ONLY"); const [estimatedVolumes, setEstimatedVolumes] = useState(5); const [runChapterBudget, setRunChapterBudget] = useState(20); const [runTokenBudget, setRunTokenBudget] = useState(""); const [continuousProgression, setContinuousProgression] = useState(true); const [continuousVolumeProgression, setContinuousVolumeProgression] = useState(false); const [allowCompletionProposal, setAllowCompletionProposal] = useState(true); const [allowAutoComplete, setAllowAutoComplete] = useState(false);
  const [theme, setTheme] = useState(""); const [plotDirection, setPlotDirection] = useState(""); const [protagonistName, setProtagonistName] = useState(""); const [protagonistGoal, setProtagonistGoal] = useState(""); const [endingDirection, setEndingDirection] = useState("");
  const [modelDraft, setModelDraft] = useState({ provider: "openai_compatible", base_url: "https://tokenrhythm.studio/v1", model: "deepseek-v4-pro", api_key: "" });
  const [modelState, setModelState] = useState<"idle" | "testing" | "ready" | "error">("idle"); const [modelMessage, setModelMessage] = useState("");
  const load = () => { setLoading(true); void api("/projects").then((data) => setProjects(data as Project[])).catch(() => setError("暂时无法读取小说库，请检查 API 服务。" )).finally(() => setLoading(false)); };
  useEffect(load, []);
  useEffect(() => {
    const controller = new AbortController();
    if (!projects.length) { setAutoProjectId(""); setActiveRunId(""); setActiveRunMode(""); setActiveRunHref(""); return; }
    void Promise.all(projects.slice(0, 20).map(async (project) => {
      const runs = await api(`/projects/${project.id}/auto-director/runs?limit=20&step_limit=1`, { signal: controller.signal }).catch(() => []);
      return (runs as Array<RunSummary & { updated_at?: string }>).filter((run) => ["CREATED", "RUNNING", "PAUSED", "FAILED", "BLOCKED"].includes(run.status || "")).map((run) => ({ projectId: project.id, updated_at: run.updated_at || "", id: run.id, run_mode: run.run_mode, href: directorHref(project.id, run) }));
    })).then((groups) => {
      if (controller.signal.aborted) return;
      const latest = groups.flat().sort((a, b) => `${b.updated_at}-${b.id}`.localeCompare(`${a.updated_at}-${a.id}`))[0];
      setAutoProjectId(latest?.projectId || ""); setActiveRunId(latest?.id || ""); setActiveRunMode(latest?.run_mode || ""); setActiveRunHref(latest?.href || "");
    }).catch(() => undefined);
    return () => controller.abort();
  }, [projects]);
  const direction = directions[selected] || blankDirection;
  const updateDirection = (key: string, value: unknown) => setDirections((items) => items.map((item, index) => index === selected ? { ...item, [key]: value } : item));
  const updateProtagonist = (key: string, value: unknown) => setDirections((items) => items.map((item, index) => index === selected ? { ...item, protagonist: { ...(item.protagonist || {}), [key]: value } } : item));
  const textItems = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);
  const seed = (mode === "inspiration" || mode === "blank" ? inspiration : source).trim();
  function openWizard() {
    setShowWizard(true); setStep("form");
    window.setTimeout(() => document.getElementById("new-novel")?.scrollIntoView({ behavior: "smooth" }), 0);
  }
  function validateCreation() {
    if (!seed && !plotDirection.trim() && !genre.trim() && !theme.trim()) return "请先填写灵感、已有材料、主题或剧情走向。";
    if (name.trim().length > 200) return "小说名称请控制在 200 字以内。";
    if (seed.length > 12000 || plotDirection.trim().length > 12000 || endingDirection.trim().length > 12000) return "灵感、材料、剧情走向和结局方向请分别控制在 12000 字以内。";
    if (theme.trim().length > 4000) return "主题请控制在 4000 字以内。";
    if (!Number.isInteger(words) || words < 500 || words > 20000) return "每章目标字数需要是 500–20000 的整数。";
    if ((workflow !== "author" || lengthMode !== "OPEN_ENDED") && (!Number.isInteger(chapters) || chapters < 3 || chapters > 500)) return "预计章节数需要是 3–500 的整数。";
    const chapterBudgetLimit = 100;
    if (workflow !== "planning" && (!Number.isInteger(runChapterBudget) || runChapterBudget < 1 || runChapterBudget > chapterBudgetLimit)) return `本次生成章节数需要是 1–${chapterBudgetLimit} 的整数。`;
    if (workflow === "author" && (!Number.isInteger(estimatedVolumes) || estimatedVolumes < 1)) return "卷数预估需要是大于 0 的整数。";
    if (workflow !== "planning" && runTokenBudget.trim() && (!Number.isInteger(Number(runTokenBudget)) || Number(runTokenBudget) < 1 || Number(runTokenBudget) > 10000000)) return "token 预算留空表示不限；填写时需要是 1–10000000 的整数。";
    return "";
  }
  async function prepareProject() {
    const body = { name: name.trim() || "未命名小说", story_seed: seed || plotDirection.trim() || theme.trim() || genre.trim(), creation_mode: "AUTONOMOUS", target_chapter_words: words };
    const project = await api(projectId ? `/projects/${projectId}` : "/projects", { method: projectId ? "PATCH" : "POST", body: JSON.stringify(body) }) as Project;
    setProjectId(project.id);
    setProjects((items) => [project, ...items.filter((item) => item.id !== project.id)]);
    if (modelState === "ready") {
      await api(`/projects/${project.id}/model-config`, { method: "PUT", body: JSON.stringify({ provider: modelDraft.provider, base_url: modelDraft.base_url, single_model_mode: true, shared_model: modelDraft.model, character_model: modelDraft.model, world_model: modelDraft.model, director_model: modelDraft.model, writer_model: modelDraft.model, critic_model: modelDraft.model, repair_model: modelDraft.model, api_key: modelDraft.api_key || undefined }) });
    }
    return project;
  }
  async function generate(event: FormEvent) {
    event.preventDefault();
    const validation = validateCreation();
    if (validation) { setError(validation); return; }
    setBusy(true); setError("");
    try {
      const project = await prepareProject();
      const samples = textItems(styleSamples); const constraints = textItems(forbidden);
      const premise = seed || plotDirection.trim() || theme.trim() || genre.trim();
      const tokenBudget = runTokenBudget.trim() ? Number(runTokenBudget) : null;
      const framing = { mode, inspiration: premise, source_material: source.trim(), genre, audience, target_chapters: chapters, target_words_per_chapter: words, pov, tone, style_samples: samples, forbidden_content: constraints };
      if (workflow === "author") {
        await api(`/projects/${project.id}/author-guided-volume/runs`, { method: "POST", body: JSON.stringify({
          ...framing, title: name.trim() || undefined, theme: theme.trim() || genre, premise,
          global_plot_direction: plotDirection.trim() || premise, ending_direction: endingDirection.trim() || undefined,
          protagonist_contract: { name: protagonistName.trim(), desire: protagonistGoal.trim() },
          style_contract: { tone, pov, audience, genre, input_mode: mode, source_material: source.trim(), style_samples: samples, forbidden_content: constraints },
          global_forbidden_events: constraints, author_locked_constraints: constraints,
          estimated_chapters: lengthMode === "OPEN_ENDED" ? null : chapters, estimated_volumes: estimatedVolumes,
          operational_run_chapter_budget: runChapterBudget, operational_token_budget: tokenBudget,
          continuous_chapter_progression: continuousProgression,
          continuous_volume_progression: continuousVolumeProgression,
          auto_cross_volume: continuousVolumeProgression,
          length_policy: { mode: lengthMode, estimated_chapters: lengthMode === "OPEN_ENDED" ? null : chapters, estimated_volumes: estimatedVolumes, completion_strategy: lengthMode === "AUTHOR_DEFINED" ? "AUTHOR_CONFIRMATION" : "MANUAL_CLOSE", operational_run_chapter_budget: runChapterBudget, operational_token_budget: tokenBudget, continuous_chapter_progression: continuousProgression, continuous_volume_progression: continuousVolumeProgression, auto_cross_volume: continuousVolumeProgression, allow_completion_proposal: allowCompletionProposal, allow_auto_complete: allowAutoComplete },
          window_size: Math.min(5, runChapterBudget), idempotency_key: `author-ui-${project.id}`,
        }) });
        location.href = `/projects/${project.id}/volumes`; return;
      }
      if (workflow === "auto") {
        await api(`/projects/${project.id}/auto-director/runs`, { method: "POST", body: JSON.stringify({ ...framing, name: name.trim() || undefined, theme: theme.trim(), global_plot_direction: plotDirection.trim(), ending_direction: endingDirection.trim(), protagonist_contract: { name: protagonistName.trim(), desire: protagonistGoal.trim() }, max_chapters: runChapterBudget, max_tokens: tokenBudget || 0, require_direction_confirmation: true, idempotency_key: `ui-${project.id}` }) });
        location.href = `/projects/${project.id}/director/auto`; return;
      }
      const result = await api(`/projects/${project.id}/creation-directions`, { method: "POST", body: JSON.stringify(framing) }) as any;
      setDirections(result.directions || []); setSelected(0); setStep("directions");
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法开始创作，请重试；已创建的小说会继续使用。"); }
    finally { setBusy(false); }
  }
  async function testModel() { setModelState("testing"); setModelMessage(""); try { const result = await api("/model-config/test-generation", { method: "POST", body: JSON.stringify({ ...modelDraft, test_prompt: '{"phase":"author_setup","ok":true}' }) }) as any; setModelState("ready"); setModelMessage(`连接成功 · ${result.model} · ${result.latency_ms}ms`); } catch (reason) { setModelState("error"); setModelMessage(reason instanceof ApiError ? reason.message : "暂时无法连接，请检查地址、模型名和密钥"); } }
  async function enterPlanning() { if (!projectId) return; setBusy(true); setError(""); try { if (!name.trim() && direction.title) await api(`/projects/${projectId}`, { method: "PATCH", body: JSON.stringify({ name: direction.title }) }); await api(`/projects/${projectId}/planning/generate`, { method: "POST", body: JSON.stringify({ premise: direction.premise || seed, framing: { mode, inspiration: seed, source_material: source.trim(), genre, audience, target_chapters: chapters, target_words_per_chapter: words, pov, ending_known: Boolean(direction.ending), tone, style_samples: textItems(styleSamples), forbidden_content: textItems(forbidden) }, style_guide: { tone: direction.style_advice || tone, direction, style_samples: textItems(styleSamples) }, anti_ai_rules: { forbidden_patterns: ["首先其次最后", "不禁", "仿佛", "命运的齿轮"] } }) }); location.href = `/projects/${projectId}/planning`; } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法生成整本规划"); } finally { setBusy(false); } }
  return <main className="library-shell">
    <header className="library-header"><div className="home-brand"><BrainCircuit size={27} /><div><strong>AI 长篇小说工作台</strong><span>先规划整本书，再逐章写作</span></div></div><div className="library-actions"><span className="library-status"><span className="status-dot" />本地单用户工作区</span><button className="library-new" onClick={openWizard}><Plus size={16} />开始新小说</button>{activeRunHref && <a className="button secondary" href={activeRunHref}>继续写作</a>}</div></header>
    <section className="library-hero"><div className="hero-intro"><p className="eyebrow"><Sparkles size={13}/> LONG-FORM NOVEL DESK</p><h1>让每一个灵感，<br/><em>都有自己的长篇。</em></h1><p className="hero-copy">从开书到封卷，让人物记住过去，让故事自然向前。<br/>你掌舵，AI 协助规划、写作与连续性检查。</p><div className="hero-links"><button onClick={openWizard}>开始创作<ArrowRight size={16}/></button><button onClick={() => setShowTour(true)}><Eye size={15}/>看看它如何工作</button><button onClick={() => setShowModelSetup(true)}><Settings2 size={15}/>测试模型</button></div><div className="hero-trust"><span><BookOpen size={14}/>作者主导</span><span><ShieldCheck size={14}/>版本可回溯</span><span><Radio size={14}/>实时创作状态</span></div></div><CreationShowcase projects={projects} selectedProjectId={autoProjectId} selectedRunId={activeRunId} selectedRunMode={activeRunMode}/></section>
    <section className="library-content" id="novels"><div className="section-kicker"><div><p className="eyebrow">YOUR NOVELS</p><h2>小说库</h2></div><span>{projects.length} 部小说</span></div>{error && step === "form" && <ErrorState message={error} retry={() => setError("")} />}{loading ? <LoadingState /> : projects.length ? <div className="library-grid">{projects.map((project, index) => <a className="world-card" href={`/projects/${project.id}`} key={project.id}><div className="world-card-top"><span className="world-index">{String(index + 1).padStart(2, "0")}</span><StatusBadge value={displayStatus(project.status)} /></div><h3>{project.name}</h3><p>{project.story_seed || "进入小说工作台继续规划。"}</p><div className="world-card-footer"><span><Clock3 size={13} />{project.updated_at ? new Date(project.updated_at).toLocaleDateString("zh-CN") : "刚刚建立"}</span><ArrowRight size={16} /></div></a>)}</div> : <EmptyState title="还没有小说" description="从一句灵感开始建立你的第一部长篇小说。" />}</section>
    <section className="feature-deck"><div className="section-kicker"><div><p className="eyebrow">WHAT YOU CAN DO</p><h2>先看清楚，再开始写</h2></div><span>四个核心工作台</span></div><div className="feature-deck-grid"><article><span className="feature-icon feature-icon-blue"><FileText size={19} /></span><div><h3>整本规划</h3><p>从灵感、大纲或正文出发，生成卷、故事弧、章节任务和伏笔回收点。</p></div></article><article><span className="feature-icon feature-icon-green"><Users size={19} /></span><div><h3>角色 Agent</h3><p>每个角色有自己的目标、记忆、已知和错误认知，行动前先过一致性检查。</p></div></article><article><span className="feature-icon feature-icon-amber"><Radio size={19} /></span><div><h3>AI 创作实况</h3><p>写作、审核、修复进行时，随时查看 AI 正在处理哪一步和下一步会做什么。</p></div></article><article><span className="feature-icon feature-icon-rose"><ShieldCheck size={19} /></span><div><h3>连续性与文风</h3><p>章节完成后检查时间线、角色知识、重复表达和 AI 痕迹，再决定是否采用正文。</p></div></article></div><div className="feature-deck-actions"><button className="button" onClick={openWizard}><Sparkles size={16} />从一句灵感开始</button><button className="button secondary" onClick={() => setShowTour(true)}><Eye size={16} />查看一次完整流程</button><button className="button secondary" onClick={() => setShowModelSetup(true)}><Settings2 size={16} />先测试模型连接</button></div></section>
    {showWizard && <section className="new-world-layout novel-wizard" id="new-novel"><div className="new-world-intro"><p className="eyebrow">NEW NOVEL · {step === "form" ? "01 / 02" : "02 / 02"}</p><h2>{step === "form" ? <>先决定<br /><em>从哪里开始。</em></> : <>选择一条<br /><em>能走完整本的路。</em></>}</h2><p>{step === "form" ? "这里不填写正文，也不需要 JSON。告诉平台你已有多少素材，剩下的由整本规划器协助完成。" : "三套方向都可以修改。你确认后才生成可审阅的卷、故事弧和章节任务单。"}</p>{step === "form" && <div className="feature-list"><span><BookOpen size={16} />作者主导，按卷验收</span><span><Users size={16} />保留你的主角与创作边界</span><span><ShieldCheck size={16} />审核通过后采用正文</span></div>}</div>
      {step === "form" ? <form className="new-world-form wizard-form" onSubmit={generate}>
        <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 16, minWidth: 0 }}>
          <div><p className="eyebrow">选择创作方式</p><div className="wizard-mode-grid">
            <button type="button" aria-pressed={workflow === "author"} className={`wizard-mode ${workflow === "author" ? "active" : ""}`} onClick={() => setWorkflow("author")}><BookOpen size={18} /><span><strong>作者主导 · 推荐</strong><small>你定方向和人物；可按作品授权自动跨卷，书籍完结由你决定。</small></span></button>
            <button type="button" aria-pressed={workflow === "auto"} className={`wizard-mode ${workflow === "auto" ? "active" : ""}`} onClick={() => setWorkflow("auto")}><Sparkles size={18} /><span><strong>自动生产</strong><small>先确认整本方向，再自动完成本次章节、审核与修复。</small></span></button>
            <button type="button" aria-pressed={workflow === "planning"} className={`wizard-mode ${workflow === "planning" ? "active" : ""}`} onClick={() => setWorkflow("planning")}><FileText size={18} /><span><strong>只做整本规划</strong><small>比较并编辑三套方向，审阅规划后再开始写作。</small></span></button>
          </div></div>
          <div><p className="eyebrow">你已有的材料</p><div className="wizard-mode-grid">{modes.map(({ id, title, description, icon: Icon }) => <button type="button" aria-pressed={mode === id} className={`wizard-mode ${mode === id ? "active" : ""}`} key={id} onClick={() => setMode(id)}><Icon size={18} /><span><strong>{title}</strong><small>{description}</small></span></button>)}</div></div>
          <label className="field"><span>小说名称 <small>可留空</small></span><input maxLength={200} value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：雾港来信" /></label>
          <label className="field full"><span>{mode === "inspiration" || mode === "blank" ? "灵感 / 故事前提" : mode === "outline" ? "已有大纲" : mode === "prose" ? "已有正文片段" : "世界观资料"}</span><textarea maxLength={12000} required={mode !== "blank"} value={mode === "inspiration" || mode === "blank" ? inspiration : source} onChange={(event) => mode === "inspiration" || mode === "blank" ? setInspiration(event.target.value) : setSource(event.target.value)} placeholder={mode === "inspiration" || mode === "blank" ? "想写一个怎样的故事？空白创作也可以先填写下面的主题与类型。" : "粘贴已有材料，AI 会把它作为创作依据。"} /></label>
          <div className="form-grid">
            <label className="field"><span>类型 / 题材</span><input value={genre} onChange={(event) => setGenre(event.target.value)} placeholder="悬疑、科幻、历史……" /></label>
            <label className="field"><span>主题 <small>可选</small></span><input maxLength={4000} value={theme} onChange={(event) => setTheme(event.target.value)} placeholder="例如：记忆与身份、信任的代价" /></label>
            <label className="field"><span>目标读者</span><input value={audience} onChange={(event) => setAudience(event.target.value)} /></label>
            <label className="field"><span>每章目标字数</span><input type="number" min={500} max={20000} step={1} value={words} onChange={(event) => setWords(Number(event.target.value))} /></label>
            <label className="field"><span>叙事视角</span><select value={pov} onChange={(event) => setPov(event.target.value)}><option value="THIRD_PERSON_LIMITED">第三人称限知</option><option value="FIRST_PERSON">第一人称</option><option value="THIRD_PERSON_OMNISCIENT">第三人称全知</option></select></label>
            <label className="field"><span>整体文风</span><input value={tone} onChange={(event) => setTone(event.target.value)} /></label>
          </div>
          {workflow !== "planning" && <>
            <label className="field full"><span>总体剧情走向 <small>可选，未填时沿用灵感或材料</small></span><textarea maxLength={12000} value={plotDirection} onChange={(event) => setPlotDirection(event.target.value)} placeholder="希望故事如何发展？哪些核心转折由你决定？" /></label>
            <div className="form-grid"><label className="field"><span>主角姓名 <small>可选</small></span><input maxLength={200} value={protagonistName} onChange={(event) => setProtagonistName(event.target.value)} /></label><label className="field"><span>主角目标 <small>可选</small></span><input value={protagonistGoal} onChange={(event) => setProtagonistGoal(event.target.value)} placeholder="想要什么，失败会付出什么代价？" /></label></div>
            <label className="field full"><span>结局方向 <small>可选</small></span><input maxLength={12000} value={endingDirection} onChange={(event) => setEndingDirection(event.target.value)} placeholder="已确定的终点，或希望保留的开放问题" /></label>
          </>}
          <details><summary>篇幅与本次运行</summary><div className="form-grid" style={{ marginTop: 12 }}>
            {workflow === "author" && <label className="field"><span>长度策略</span><select value={lengthMode} onChange={(event) => setLengthMode(event.target.value)}><option value="OPEN_ENDED">开放长度</option><option value="ESTIMATE_ONLY">篇幅仅作估算</option><option value="AUTHOR_DEFINED">由作者决定完结</option></select></label>}
            <label className="field"><span>{workflow === "author" ? "章节数预估" : "整本规划章节数"}</span><input type="number" min={3} max={500} step={1} disabled={workflow === "author" && lengthMode === "OPEN_ENDED"} value={chapters} onChange={(event) => setChapters(Number(event.target.value))} /></label>
            {workflow === "author" && <label className="field"><span>卷数预估</span><input type="number" min={1} step={1} value={estimatedVolumes} onChange={(event) => setEstimatedVolumes(Number(event.target.value))} /></label>}
            {workflow !== "planning" && <><label className="field"><span>本次最多生成章节数</span><input type="number" min={1} max={100} step={1} value={runChapterBudget} onChange={(event) => setRunChapterBudget(Number(event.target.value))} /></label><label className="field"><span>本次 token 预算 <small>留空表示不限</small></span><input type="number" min={1} max={10000000} step={1} value={runTokenBudget} onChange={(event) => setRunTokenBudget(event.target.value)} placeholder="不限" /></label></>}
          </div>{workflow === "author" && <><p className="muted">篇幅预估不作为完结上限。系统每 {Math.min(5, runChapterBudget)} 章滚动规划；预算内自动接上下一批，达到章节或 token 预算后暂停，随时可继续。自动跨卷只会在当前卷通过检查并保存连续性快照后发生。</p><label className="checkbox-field"><input type="checkbox" checked={continuousProgression} onChange={(event) => setContinuousProgression(event.target.checked)} />自动跨窗口连续推进（默认开启）</label><label className="checkbox-field"><input type="checkbox" checked={continuousVolumeProgression} onChange={(event) => setContinuousVolumeProgression(event.target.checked)} />自动跨卷连续推进（默认关闭）</label><label className="checkbox-field"><input type="checkbox" checked={allowCompletionProposal} onChange={(event) => setAllowCompletionProposal(event.target.checked)} />允许提出完结建议</label><label className="checkbox-field"><input type="checkbox" checked={allowAutoComplete} onChange={(event) => setAllowAutoComplete(event.target.checked)} />允许 AI 在满足合同条件后自动完结</label></>}</details>
          <label className="field full"><span>认可的文风样本 <small>可选，每段一行</small></span><textarea value={styleSamples} onChange={(event) => setStyleSamples(event.target.value)} placeholder="粘贴你认可的中文文风。" /></label>
          <label className="field full"><span>禁止出现的内容 <small>可选，每项一行</small></span><textarea value={forbidden} onChange={(event) => setForbidden(event.target.value)} placeholder="不想出现的主题、桥段、表达或尺度边界。" /></label>
          <p className="form-note">{modelState === "ready" ? "将使用首页已测试成功的模型配置。" : "将使用本机默认模型配置；也可先在首页测试模型。"}</p>
          {error && <ErrorState message={error} retry={() => setError("")} />}
          {projectId && <small className="form-note">本次已建立小说；重试会继续使用同一项目。</small>}
          <button type="submit" disabled={busy}>{busy ? "正在准备创作……" : workflow === "author" ? "建立书籍合同，开始当前卷" : workflow === "auto" ? "生成方向，进入自动生产" : "生成三套整本方向"}</button>
          <small className="form-note">{workflow === "author" ? "你确定方向；AI 按本次预算生成、审核与修复正文。" : workflow === "auto" ? "确认方向后会开始生成正文，可随时暂停或接管。" : "此步骤只生成规划，不生成正文。"}</small>
        </fieldset>
      </form> : <div className="direction-panel"><div className="direction-tabs">{directions.map((item, index) => <button type="button" className={selected === index ? "active" : ""} key={index} onClick={() => setSelected(index)}>方向 {index + 1}<small>{item.title || "未命名方向"}</small></button>)}</div><div className="direction-editor"><label className="field"><span>方向名称</span><input value={direction.title || ""} onChange={(e) => updateDirection("title", e.target.value)} /></label><label className="field full"><span>一句话方案</span><textarea value={direction.premise || ""} onChange={(e) => updateDirection("premise", e.target.value)} /></label><div className="form-grid"><label className="field"><span>核心卖点</span><textarea value={direction.selling_point || ""} onChange={(e) => updateDirection("selling_point", e.target.value)} /></label><label className="field"><span>核心冲突</span><textarea value={direction.core_conflict || ""} onChange={(e) => updateDirection("core_conflict", e.target.value)} /></label><label className="field"><span>故事终点</span><textarea value={direction.ending || ""} onChange={(e) => updateDirection("ending", e.target.value)} /></label><label className="field"><span>第一卷目标</span><textarea value={direction.first_volume_goal || ""} onChange={(e) => updateDirection("first_volume_goal", e.target.value)} /></label><label className="field full"><span>主角</span><input value={direction.protagonist?.name || ""} onChange={(e) => updateProtagonist("name", e.target.value)} placeholder="姓名" /><textarea value={direction.protagonist?.desire || ""} onChange={(e) => updateProtagonist("desire", e.target.value)} placeholder="主角想要什么？失败要付出什么代价？" /></label><label className="field full"><span>世界边界 <small>每行一项</small></span><textarea value={lines(direction.world_boundaries)} onChange={(e) => updateDirection("world_boundaries", e.target.value.split("\n").filter(Boolean))} /></label><label className="field full"><span>前十章承诺 <small>每行一项</small></span><textarea value={lines(direction.first_ten_chapter_promises)} onChange={(e) => updateDirection("first_ten_chapter_promises", e.target.value.split("\n").filter(Boolean))} /></label><label className="field full"><span>伏笔方向 <small>每行一项</small></span><textarea value={lines(direction.foreshadowing_directions)} onChange={(e) => updateDirection("foreshadowing_directions", e.target.value.split("\n").filter(Boolean))} /></label><label className="field full"><span>文风建议</span><textarea value={direction.style_advice || ""} onChange={(e) => updateDirection("style_advice", e.target.value)} /></label></div>{error && <ErrorState message={error} retry={() => setError("")} />}<div className="row"><button className="secondary" type="button" onClick={() => setStep("form")} disabled={busy}><ArrowLeft size={15} />重新填写</button><button type="button" onClick={enterPlanning} disabled={busy}><Check size={15} />确认方向，进入整本规划 <ArrowRight size={15} /></button></div><small className="form-note">确认后生成卷、故事弧和章节任务单，仍可逐项编辑。</small></div></div>}</section>}
    {showTour && <div className="author-dialog-backdrop" role="presentation" onClick={() => setShowTour(false)}><section className="author-dialog" role="dialog" aria-modal="true" aria-labelledby="tour-title" onClick={(event) => event.stopPropagation()}><header><div><p className="eyebrow">A NOVEL, END TO END</p><h2 id="tour-title">一部长篇会这样走完</h2></div><button className="icon-button" onClick={() => setShowTour(false)} aria-label="关闭"><X size={18} /></button></header><div className="tour-steps"><div><span>01</span><div><strong>先定整本方向</strong><p>AI 把灵感整理成可编辑的主线、人物、卷战略和章节任务。</p></div></div><div><span>02</span><div><strong>再让角色行动</strong><p>每个角色只使用自己知道的事，行动会受到目标、记忆和关系约束。</p></div></div><div><span>03</span><div><strong>最后生成正文</strong><p>章节按任务单写作，经过连续性、文风和反 AI 检查后才采用。</p></div></div></div><div className="tour-footer"><CheckCircle2 size={17} /><span>任何一步都可以暂停、修改和回到上一个检查点。</span></div></section></div>}
    {showModelSetup && <div className="author-dialog-backdrop" role="presentation" onClick={() => setShowModelSetup(false)}><section className="author-dialog model-setup-dialog" role="dialog" aria-modal="true" aria-labelledby="model-setup-title" onClick={(event) => event.stopPropagation()}><header><div><p className="eyebrow">MODEL CONNECTION</p><h2 id="model-setup-title">先确认 AI 能正常工作</h2><p className="dialog-description">这里只测试连接，不会创建小说。测试通过后，配置会带入下一本小说。</p></div><button className="icon-button" onClick={() => setShowModelSetup(false)} aria-label="关闭"><X size={18} /></button></header><div className="model-setup-grid"><label className="field full"><span>模型服务地址</span><input value={modelDraft.base_url} onChange={(event) => { setModelDraft({ ...modelDraft, base_url: event.target.value }); setModelState("idle"); }} /></label><label className="field"><span>模型名称</span><input value={modelDraft.model} onChange={(event) => { setModelDraft({ ...modelDraft, model: event.target.value }); setModelState("idle"); }} /></label><label className="field"><span>API 密钥</span><input type="password" autoComplete="new-password" value={modelDraft.api_key} onChange={(event) => { setModelDraft({ ...modelDraft, api_key: event.target.value }); setModelState("idle"); }} placeholder="只在本次使用中传送，不显示在页面上" /></label></div>{modelState !== "idle" && <div className={`model-test-result ${modelState}`}><span className="status-dot" />{modelState === "testing" ? "正在测试模型连接…" : modelMessage}</div>}<footer><button className="button secondary" onClick={() => setShowModelSetup(false)}>稍后设置</button><button className="button" onClick={testModel} disabled={modelState === "testing"}><Radio size={16} />{modelState === "testing" ? "测试中…" : "测试连接"}</button></footer></section></div>}
  </main>;
}
