"use client";

import { FormEvent, useEffect, useState } from "react";
import { Check, Database, RotateCcw, Save, Zap } from "lucide-react";
import { api } from "../../../lib";
import { ErrorState, LoadingState, PageHeader, SectionCard } from "../../../../components/ui/primitives";
import styles from "./model-config.module.css";

type Config = Record<string, any>;
const editableConfigFields = [
  "provider", "base_url", "character_model", "world_model", "director_model", "writer_model", "critic_model", "repair_model", "fallback_model",
  "auto_failover", "max_repair_attempts", "embedding_enabled", "embedding_use_main_connection", "embedding_provider", "embedding_base_url", "embedding_model", "embedding_dimension",
  "memory_retrieval_mode", "memory_vector_top_k", "memory_rrf_k", "memory_semantic_min_similarity",
] as const;

function buildModelConfigWritePayload(config: Config, generationKey: string, embeddingKey: string): Config {
  const payload = Object.fromEntries(editableConfigFields.map((field) => [field, config[field]]));
  if (config.embedding_dimension === "") payload.embedding_dimension = null;
  else if (config.embedding_dimension !== undefined && config.embedding_dimension !== null) payload.embedding_dimension = Number(config.embedding_dimension);
  if (config.memory_semantic_min_similarity === "") payload.memory_semantic_min_similarity = null;
  else if (config.memory_semantic_min_similarity !== undefined && config.memory_semantic_min_similarity !== null) payload.memory_semantic_min_similarity = Number(config.memory_semantic_min_similarity);
  if (generationKey) payload.api_key = generationKey;
  if (embeddingKey) payload.embedding_api_key = embeddingKey;
  return payload;
}

const defaults: Config = {
  provider: "disabled", base_url: "", character_model: "", world_model: "", director_model: "", writer_model: "", critic_model: "", repair_model: "",
  auto_failover: false, max_repair_attempts: 1,
  embedding_enabled: false, embedding_use_main_connection: true, embedding_provider: "openai_compatible", embedding_base_url: "", embedding_model: "", embedding_dimension: "",
  memory_retrieval_mode: "DETERMINISTIC", memory_vector_top_k: 12, memory_rrf_k: 60, memory_semantic_min_similarity: "",
};

export default function ModelConfigPage({ params }: { params: { id: string } }) {
  const [config, setConfig] = useState<Config>(defaults); const [generationKey, setGenerationKey] = useState(""); const [embeddingKey, setEmbeddingKey] = useState("");
  const [indexStatus, setIndexStatus] = useState<Config | null>(null);
  const [loading, setLoading] = useState(true); const [saving, setSaving] = useState(false); const [message, setMessage] = useState(""); const [error, setError] = useState("");
  const load = () => { setLoading(true); setError(""); void Promise.all([api(`/projects/${params.id}/model-config`), api(`/projects/${params.id}/memory-embeddings/status`).catch(() => null)]).then(([value, status]) => { setConfig({ ...defaults, ...(value || {}) }); setIndexStatus(status as Config | null); }).catch(() => setError("无法读取模型配置。" )).finally(() => setLoading(false)); };
  useEffect(load, [params.id]);
  const update = (name: string, value: unknown) => setConfig((current) => ({ ...current, [name]: value }));
  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError(""); setMessage("");
    try {
      const payload = buildModelConfigWritePayload(config, generationKey, embeddingKey);
      const result = await api(`/projects/${params.id}/model-config`, { method: "PUT", body: JSON.stringify(payload) }) as Config;
      setConfig({ ...defaults, ...result }); setGenerationKey(""); setEmbeddingKey(""); setMessage("配置已保存。密钥不会再次显示。"); void api(`/projects/${params.id}/memory-embeddings/status`).then(setIndexStatus).catch(() => undefined);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败。"); } finally { setSaving(false); }
  }
  async function testEmbedding() {
    setSaving(true); setError(""); setMessage("");
    try { const result = await api(`/projects/${params.id}/model-config/test-embedding`, { method: "POST", body: JSON.stringify({ embedding_use_main_connection: Boolean(config.embedding_use_main_connection), provider: config.embedding_use_main_connection ? config.provider : config.embedding_provider, base_url: config.embedding_use_main_connection ? config.base_url : config.embedding_base_url, model: config.embedding_model, dimension: config.embedding_dimension || undefined, api_key: (config.embedding_use_main_connection ? generationKey : embeddingKey) || undefined, test_text: "embedding connectivity test" }) }) as any; update("embedding_dimension", result.dimension); setMessage(`嵌入连接可用：${result.model}，${result.dimension} 维。`); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "嵌入连接测试失败。"); } finally { setSaving(false); }
  }
  async function indexMemories(rebuild = false) {
    setSaving(true); setError("");
    try { const result = await api(`/projects/${params.id}/memory-embeddings/${rebuild ? "rebuild" : "index-missing"}`, { method: "POST" }) as any; setMessage(`索引完成：${result.indexed || 0} 条更新，${result.skipped || 0} 条跳过。`); setIndexStatus(await api(`/projects/${params.id}/memory-embeddings/status`) as Config); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "索引失败。"); } finally { setSaving(false); }
  }
  if (loading) return <LoadingState />;
  return <form className="form" onSubmit={save}><PageHeader title="模型与记忆检索" description="项目级模型连接与角色记忆召回策略。密钥仅作为写入值处理。" action={<button className="button" disabled={saving}><Save size={16} />保存</button>} />
    {error && <ErrorState message={error} retry={load} />}{message && <div className="success-state"><Check size={18} />{message}</div>}
    <div className={styles.columns}><SectionCard title="生成模型" description="角色、世界裁定、导演与写作角色共享此连接配置。"><div className="form-grid">
      <label className="field"><span>提供方</span><select value={config.provider} onChange={(e) => update("provider", e.target.value)}><option value="disabled">禁用</option><option value="openai_compatible">OpenAI 兼容</option></select></label>
      <label className="field"><span>Base URL</span><input value={config.base_url || ""} onChange={(e) => update("base_url", e.target.value)} placeholder="https://provider.example/v1" /></label>
      <label className="field"><span>角色模型</span><input value={config.character_model || ""} onChange={(e) => update("character_model", e.target.value)} /></label>
      <label className="field"><span>世界模型</span><input value={config.world_model || ""} onChange={(e) => update("world_model", e.target.value)} /></label>
      <label className="field"><span>导演模型</span><input value={config.director_model || ""} onChange={(e) => update("director_model", e.target.value)} /></label>
      <label className="field"><span>写作模型</span><input value={config.writer_model || ""} onChange={(e) => update("writer_model", e.target.value)} /></label>
      <label className="field"><span>批评模型</span><input value={config.critic_model || ""} onChange={(e) => update("critic_model", e.target.value)} /></label>
      <label className="field"><span>修复模型</span><input value={config.repair_model || ""} onChange={(e) => update("repair_model", e.target.value)} /></label>
      <label className="field"><span>新生成密钥</span><input type="password" autoComplete="new-password" value={generationKey} onChange={(e) => setGenerationKey(e.target.value)} placeholder={config.credentials?.GENERATION?.configured ? `已配置 ${config.credentials.GENERATION.hint}` : "仅写入，不会回显"} /></label>
    </div></SectionCard>
    <SectionCard title="角色记忆向量检索" description="默认保持确定性召回；启用混合模式后，向量结果只作为 RRF 候选排序。"><div className="form-grid">
      <label className="field"><span>记忆模式</span><select value={config.memory_retrieval_mode} onChange={(e) => update("memory_retrieval_mode", e.target.value)}><option value="DETERMINISTIC">确定性</option><option value="HYBRID_RRF">混合 RRF</option></select></label>
      <label className="field"><span>启用嵌入</span><select value={String(config.embedding_enabled)} onChange={(e) => update("embedding_enabled", e.target.value === "true")}><option value="false">否</option><option value="true">是</option></select></label>
      <label className="field"><span>连接来源</span><select value={String(config.embedding_use_main_connection)} onChange={(e) => update("embedding_use_main_connection", e.target.value === "true")}><option value="true">复用生成连接</option><option value="false">独立嵌入连接</option></select></label>
      {!config.embedding_use_main_connection && <><label className="field"><span>嵌入提供方</span><select value={config.embedding_provider || "openai_compatible"} onChange={(e) => update("embedding_provider", e.target.value)}><option value="openai_compatible">OpenAI 兼容</option></select></label><label className="field"><span>嵌入 Base URL</span><input value={config.embedding_base_url || ""} onChange={(e) => update("embedding_base_url", e.target.value)} /></label></>}
      <label className="field"><span>嵌入模型</span><input value={config.embedding_model || ""} onChange={(e) => update("embedding_model", e.target.value)} /></label>
      <label className="field"><span>向量维度</span><input type="number" min="1" value={config.embedding_dimension ?? ""} onChange={(e) => update("embedding_dimension", e.target.value)} /></label>
      {!config.embedding_use_main_connection && <label className="field"><span>新嵌入密钥</span><input type="password" autoComplete="new-password" value={embeddingKey} onChange={(e) => setEmbeddingKey(e.target.value)} placeholder={config.credentials?.EMBEDDING?.configured ? `已配置 ${config.credentials.EMBEDDING.hint}` : "仅写入，不会回显"} /></label>}
      <label className="field"><span>向量候选数</span><input type="number" min="1" value={config.memory_vector_top_k} onChange={(e) => update("memory_vector_top_k", Number(e.target.value))} /></label>
      <label className="field"><span>RRF 常量</span><input type="number" min="1" value={config.memory_rrf_k} onChange={(e) => update("memory_rrf_k", Number(e.target.value))} /></label>
    </div><div className="button-row"><button className="button secondary" type="button" disabled={saving} onClick={testEmbedding}><Zap size={16} />测试嵌入连接</button><button className="button secondary" type="button" disabled={saving} onClick={() => indexMemories(false)}><Database size={16} />索引缺失记忆</button><button className="button secondary" type="button" disabled={saving} onClick={() => indexMemories(true)}><RotateCcw size={16} />重建索引</button></div>{indexStatus && <div className="metric-grid"><div><strong>{indexStatus.current_valid_memory_count}</strong><span>当前有效记忆</span></div><div><strong>{indexStatus.ready_count}</strong><span>已建立</span></div><div><strong>{indexStatus.missing_count}</strong><span>缺失</span></div><div><strong>{indexStatus.failed_count}</strong><span>失败</span></div><div><strong>{Math.round((indexStatus.coverage_ratio || 0) * 100)}%</strong><span>覆盖率</span></div><div><strong>{indexStatus.dimension || "-"}</strong><span>向量维度</span></div></div>}</SectionCard></div>
  </form>;
}
