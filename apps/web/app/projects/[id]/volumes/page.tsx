"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  BookOpen,
  Lock,
  Play,
  Plus,
  RotateCcw,
} from "lucide-react";
import { api, ApiError } from "../../../lib";
import {
  ErrorState,
  LoadingState,
  PageHeader,
  SectionCard,
  StatusBadge,
} from "../../../../components/ui/primitives";
import {
  createLivePoller,
  type LiveSyncState,
} from "../../../../components/live/live-poller";

type Volume = {
  id: string;
  volume_number: number;
  title?: string;
  status: string;
  estimated_chapter_start?: number;
  estimated_chapter_end?: number;
  actual_chapter_start?: number;
  actual_chapter_end?: number;
  volume_goal?: string;
  core_conflict?: string;
  target_closing_state?: Record<string, unknown>;
  completion_conditions?: unknown[];
  progress?: {
    volume_goal_progress?: number;
    should_prepare_seal?: boolean;
    unresolved_threads?: unknown[];
  };
  auto_cross_volume_checkpoint?: {
    status: string;
    run_id: string;
    source_snapshot_id: string;
    next_volume_id: string;
    missing_fields: string[];
  } | null;
  source_snapshot?: {
    id: string;
    available: boolean;
    version?: number;
    summary?: string | null;
    source_fingerprint?: string;
    formal_world_fingerprint?: string;
    chapter_start?: number;
    chapter_end?: number;
    adopted_chapter_count?: number;
    confirmed_fact_count?: number;
    character_count?: number;
    active_thread_count?: number;
    next_volume_hooks?: unknown[];
  } | null;
  windows?: Array<{
    id: string;
    start_chapter_number: number;
    end_chapter_number: number;
    status: string;
  }>;
};

type SuccessorPlanDraft = {
  goal: string;
  conflict: string;
  closingState: string;
};

const missingFieldLabels: Record<string, string> = {
  volume_goal: "下一卷目标",
  core_conflict: "核心冲突",
  target_closing_state: "结束状态",
};

export default function VolumesPage({ params }: { params: { id: string } }) {
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [editingVolumeId, setEditingVolumeId] = useState<string | null>(null);
  const [planDraft, setPlanDraft] = useState<SuccessorPlanDraft>({
    goal: "",
    conflict: "",
    closingState: "{}",
  });
  const [planError, setPlanError] = useState("");
  const pollerRef = useRef<ReturnType<typeof createLivePoller> | null>(null);
  const [sync, setSync] = useState<LiveSyncState>({
    status: "paused",
    lastUpdatedAt: null,
    retryAt: null,
    failures: 0,
  });
  useEffect(() => {
    let cancelled = false;
    const poller = createLivePoller({
      intervalMs: 2000,
      load: async (signal) => {
        const value: unknown = await api(`/projects/${params.id}/volumes`, {
          signal,
        });
        if (signal.aborted || cancelled) return;
        if (!Array.isArray(value)) throw new Error("INVALID_VOLUMES_RESPONSE");
        setVolumes(value as Volume[]);
      },
      onState: (state) => {
        if (!cancelled) setSync(state);
      },
    });
    pollerRef.current = poller;
    const updateVisibility = () => poller.setPaused(document.hidden);
    document.addEventListener("visibilitychange", updateVisibility);
    updateVisibility();
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", updateVisibility);
      poller.dispose();
      if (pollerRef.current === poller) pollerRef.current = null;
    };
  }, [params.id]);
  function showActionError(reason: unknown, fallback: string) {
    setError(reason instanceof ApiError ? reason.message : fallback);
  }
  async function action(
    volume: Volume,
    path: "start" | "continue" | "extend" | "seal",
    body: Record<string, unknown> = {},
  ) {
    setBusy(true);
    setError("");
    try {
      await api(`/projects/${params.id}/volumes/${volume.id}/${path}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      pollerRef.current?.refresh();
    } catch (reason: unknown) {
      showActionError(reason, "卷操作失败");
    } finally {
      setBusy(false);
    }
  }
  async function startNextVolume(volume: Volume) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = (await api(
        `/projects/${params.id}/volumes/${volume.id}/next`,
        { method: "POST" },
      )) as { volume: { id: string; volume_number: number } };
      await api(`/projects/${params.id}/volumes/${result.volume.id}/start`, {
        method: "POST",
      });
      setMessage(
        `第 ${result.volume.volume_number} 卷已开始，继续推进后续章节。`,
      );
      pollerRef.current?.refresh();
    } catch (reason: unknown) {
      showActionError(reason, "无法开始下一卷");
    } finally {
      setBusy(false);
    }
  }
  function editSuccessorPlan(volume: Volume) {
    setEditingVolumeId(volume.id);
    setPlanError("");
    setPlanDraft({
      goal: volume.volume_goal || "",
      conflict: volume.core_conflict || "",
      closingState: JSON.stringify(volume.target_closing_state || {}, null, 2),
    });
  }
  async function resumeSuccessorRun(volume: Volume, savePlan: boolean) {
    const checkpoint = volume.auto_cross_volume_checkpoint;
    if (!checkpoint) return;
    setBusy(true);
    setError("");
    setPlanError("");
    setMessage("");
    try {
      if (savePlan) {
        if (!planDraft.goal.trim() || !planDraft.conflict.trim()) {
          setPlanError("请填写下一卷目标和核心冲突。");
          return;
        }
        let closingState: unknown;
        try {
          closingState = JSON.parse(planDraft.closingState);
        } catch {
          setPlanError("结束状态必须是有效的 JSON 对象。");
          return;
        }
        if (
          !closingState ||
          typeof closingState !== "object" ||
          Array.isArray(closingState) ||
          !Object.keys(closingState).length
        ) {
          setPlanError("结束状态需要填写至少一个可核对的正式世界字段。");
          return;
        }
        await api(`/projects/${params.id}/volumes/${volume.id}/contract`, {
          method: "PATCH",
          body: JSON.stringify({
            volume_goal: planDraft.goal,
            core_conflict: planDraft.conflict,
            target_closing_state: closingState,
            author_note: "补齐自动跨卷下一卷规划",
          }),
        });
      }
      await api(
        `/projects/${params.id}/author-guided-volume/runs/${checkpoint.run_id}/continue`,
        { method: "POST" },
      );
      setEditingVolumeId(null);
      setMessage(
        `第 ${volume.volume_number} 卷规划已${savePlan ? "保存并" : ""}从原运行检查点继续，剩余预算沿用。`,
      );
      pollerRef.current?.refresh();
    } catch (reason: unknown) {
      showActionError(reason, "无法恢复原自动运行");
    } finally {
      setBusy(false);
    }
  }
  const syncLabel =
    sync.status === "paused"
      ? "后台暂停同步"
      : sync.status === "syncing"
        ? "正在同步卷状态"
        : sync.status === "offline"
          ? "连接暂断，自动重试中"
          : "卷状态已同步";
  const lastUpdated = sync.lastUpdatedAt
    ? new Date(sync.lastUpdatedAt).toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "尚未同步";
  return (
    <main className="stack">
      <PageHeader
        title="卷总览"
        description="预估范围只用于规划参考；实际卷长和章节数由剧情完成条件与作者确认决定。"
        action={
          <Link
            className="button secondary"
            href={`/projects/${params.id}/director/volume`}
          >
            <BookOpen size={15} />
            当前卷导演
          </Link>
        }
      />
      <div
        className={`ai-live-sync${sync.status === "offline" ? " ai-live-sync-offline" : ""}`}
        aria-live="polite"
      >
        <span>
          {syncLabel}
          {sync.status === "offline" && volumes.length > 0
            ? " · 保留已显示数据"
            : ""}
        </span>
        <span className="ai-live-sync-meta">最近更新：{lastUpdated}</span>
        <button
          className="ai-live-refresh"
          onClick={() => pollerRef.current?.refresh()}
          disabled={sync.status === "paused" || sync.status === "syncing"}
          aria-label={
            sync.status === "offline" ? "立即重试同步卷状态" : "刷新卷状态"
          }
        >
          <RotateCcw size={14} />
          {sync.status === "offline" ? "立即重试" : "刷新"}
        </button>
      </div>
      {error && (
        <ErrorState
          message={error}
          retry={() => {
            setError("");
            pollerRef.current?.refresh();
          }}
        />
      )}
      {message && (
        <div className="success-state" role="status">
          {message}
        </div>
      )}
      {!volumes.length && !sync.lastUpdatedAt && sync.status !== "offline" ? (
        <LoadingState />
      ) : !volumes.length ? (
        <p className="muted">
          {sync.status === "offline"
            ? "暂时无法同步卷总览，请重试。"
            : "当前还没有卷。"}
        </p>
      ) : (
        <div className="stack">
          {volumes.map((volume) => (
            <SectionCard
              key={volume.id}
              title={`第 ${volume.volume_number} 卷 · ${volume.title || "未命名"}`}
              aside={<StatusBadge value={volume.status} />}
            >
              <div className="run-metrics">
                <span>
                  预估范围：{volume.estimated_chapter_start || "-"} -{" "}
                  {volume.estimated_chapter_end || "-"}
                </span>
                <span>
                  实际范围：{volume.actual_chapter_start || "-"} -{" "}
                  {volume.actual_chapter_end || "-"}
                </span>
                <span>
                  目标进度：
                  {Math.round(
                    (volume.progress?.volume_goal_progress || 0) * 100,
                  )}
                  %
                </span>
                <span>
                  未回收线程：{volume.progress?.unresolved_threads?.length || 0}
                </span>
              </div>
              <p className="muted">
                {volume.volume_goal || "当前卷尚未填写目标。"}
              </p>
              {volume.source_snapshot && (
                <div className="continuity-reference">
                  <strong>承接快照</strong>
                  {volume.source_snapshot.available ? (
                    <>
                      <p>
                        {volume.source_snapshot.summary ||
                          "本卷从已封存的正式世界状态继续。"}
                      </p>
                      <div className="run-metrics">
                        <span>
                          来源卷：{volume.volume_number - 1} · 快照 v
                          {volume.source_snapshot.version}
                        </span>
                        <span>
                          采用章节：
                          {volume.source_snapshot.chapter_start || "-"}–
                          {volume.source_snapshot.chapter_end || "-"}（
                          {volume.source_snapshot.adopted_chapter_count || 0}{" "}
                          章）
                        </span>
                        <span>
                          确认事实：
                          {volume.source_snapshot.confirmed_fact_count || 0}
                        </span>
                        <span>
                          人物状态：
                          {volume.source_snapshot.character_count || 0}
                        </span>
                        <span>
                          活跃线程：
                          {volume.source_snapshot.active_thread_count || 0}
                        </span>
                      </div>
                      <code className="continuity-fingerprint">
                        快照 {volume.source_snapshot.id} · 状态指纹{" "}
                        {volume.source_snapshot.formal_world_fingerprint ||
                          volume.source_snapshot.source_fingerprint ||
                          "未提供"}
                      </code>
                      {Boolean(
                        volume.source_snapshot.next_volume_hooks?.length,
                      ) && (
                        <p>
                          承接钩子：
                          {volume.source_snapshot.next_volume_hooks
                            ?.map(String)
                            .join("；")}
                        </p>
                      )}
                    </>
                  ) : (
                    <p role="alert">承接快照不可读取，不能从此状态继续。</p>
                  )}
                </div>
              )}
              {volume.auto_cross_volume_checkpoint && (
                <div
                  className="cross-volume-checkpoint"
                  role="group"
                  aria-label={`第 ${volume.volume_number} 卷跨卷续写检查点`}
                >
                  <h3>跨卷续写检查点</h3>
                  <p>
                    沿用原自动运行与剩余预算。来源快照需与上一卷封存版本一致。
                  </p>
                  <div className="successor-contract-summary">
                    <div>
                      <small>下一卷目标</small>
                      <p>{volume.volume_goal || "待补"}</p>
                    </div>
                    <div>
                      <small>核心冲突</small>
                      <p>{volume.core_conflict || "待补"}</p>
                    </div>
                    <div>
                      <small>结束条件</small>
                      {volume.target_closing_state &&
                      Object.keys(volume.target_closing_state).length > 0 ? (
                        <pre>
                          {JSON.stringify(volume.target_closing_state, null, 2)}
                        </pre>
                      ) : volume.completion_conditions?.length ? (
                        <pre>
                          {JSON.stringify(volume.completion_conditions, null, 2)}
                        </pre>
                      ) : (
                        <p>待补</p>
                      )}
                    </div>
                  </div>
                  {volume.auto_cross_volume_checkpoint.missing_fields.length ? (
                    <>
                      <p className="field-error">
                        待补：
                        {volume.auto_cross_volume_checkpoint.missing_fields
                          .map((field) => missingFieldLabels[field] || field)
                          .join("、")}
                      </p>
                      {volume.source_snapshot?.available && (
                        <>
                          {editingVolumeId === volume.id ? (
                            <div className="form-grid successor-plan-form">
                              <label className="field full">
                                <span>下一卷目标</span>
                                <textarea
                                  value={planDraft.goal}
                                  onChange={(event) =>
                                    setPlanDraft({
                                      ...planDraft,
                                      goal: event.target.value,
                                    })
                                  }
                                />
                              </label>
                              <label className="field full">
                                <span>核心冲突</span>
                                <textarea
                                  value={planDraft.conflict}
                                  onChange={(event) =>
                                    setPlanDraft({
                                      ...planDraft,
                                      conflict: event.target.value,
                                    })
                                  }
                                />
                              </label>
                              <label className="field full">
                                <span>结束状态（JSON）</span>
                                <textarea
                                  value={planDraft.closingState}
                                  onChange={(event) =>
                                    setPlanDraft({
                                      ...planDraft,
                                      closingState: event.target.value,
                                    })
                                  }
                                  spellCheck={false}
                                  aria-describedby={`closing-state-help-${volume.id}`}
                                />
                                <small id={`closing-state-help-${volume.id}`}>
                                  填写能在正式人物、世界实体、故事线程或作品字段中核对的目标状态。
                                </small>
                              </label>
                              {planError && (
                                <p className="field-error" role="alert">
                                  {planError}
                                </p>
                              )}
                              <div className="row field full">
                                <button
                                  onClick={() =>
                                    void resumeSuccessorRun(volume, true)
                                  }
                                  disabled={busy}
                                >
                                  <Play size={15} />
                                  保存并从原检查点继续
                                </button>
                                <button
                                  className="secondary"
                                  onClick={() => setEditingVolumeId(null)}
                                  disabled={busy}
                                >
                                  取消
                                </button>
                              </div>
                            </div>
                          ) : (
                            <button
                              className="secondary"
                              onClick={() => editSuccessorPlan(volume)}
                              disabled={busy}
                            >
                              补齐下一卷规划
                            </button>
                          )}
                        </>
                      )}
                    </>
                  ) : (
                    <>
                      <p>目标、冲突和结束状态已齐备。</p>
                      <button
                        onClick={() => void resumeSuccessorRun(volume, false)}
                        disabled={busy}
                      >
                        <Play size={15} />
                        从原检查点继续
                      </button>
                    </>
                  )}
                </div>
              )}
              <div className="row">
                {volume.status !== "SEALED" &&
                  !(
                    volume.auto_cross_volume_checkpoint &&
                    volume.status === "DRAFT"
                  ) && (
                    <button
                      onClick={() =>
                        void action(
                          volume,
                          volume.status === "DRAFT" ? "start" : "continue",
                        )
                      }
                      disabled={busy}
                    >
                      <Play size={15} />
                      {volume.status === "DRAFT" ? "开始本卷" : "继续本卷"}
                    </button>
                  )}
                {volume.status !== "SEALED" &&
                  !(
                    volume.auto_cross_volume_checkpoint &&
                    volume.status === "DRAFT"
                  ) && (
                    <button
                      className="secondary"
                      onClick={() =>
                        void action(volume, "extend", { window_size: 5 })
                      }
                      disabled={busy}
                    >
                      <Plus size={15} />
                      扩展当前卷
                    </button>
                  )}
                {volume.progress?.should_prepare_seal &&
                  volume.status !== "SEALED" &&
                  !(
                    volume.auto_cross_volume_checkpoint &&
                    volume.status === "DRAFT"
                  ) && (
                    <button
                      className="secondary"
                      onClick={() =>
                        void action(volume, "seal", { author_confirmed: true })
                      }
                      disabled={busy}
                    >
                      <Lock size={15} />
                      封存本卷
                    </button>
                  )}
                {volume.status === "SEALED" &&
                  !volumes.some(
                    (candidate) =>
                      candidate.volume_number === volume.volume_number + 1 &&
                      candidate.auto_cross_volume_checkpoint?.status ===
                        "AWAITING_AUTHOR_PLAN",
                  ) && (
                    <>
                      <span className="muted">
                        <Lock size={14} />
                        已封存，仅可读取快照
                      </span>
                      <button
                        className="secondary"
                        onClick={() => void startNextVolume(volume)}
                        disabled={busy}
                      >
                        <ArrowRight size={15} />
                        开始下一卷并继续写作
                      </button>
                    </>
                  )}
                <Link
                  className="button secondary"
                  href={
                    "/projects/" +
                    params.id +
                    "/director/volume?volume=" +
                    volume.id
                  }
                >
                  查看导演台 <ArrowRight size={15} />
                </Link>
              </div>
              {volume.windows?.length ? (
                <div className="completed-chapters">
                  {volume.windows.map((window) => (
                    <span key={window.id}>
                      窗口 {window.start_chapter_number}-
                      {window.end_chapter_number} · {window.status}
                    </span>
                  ))}
                </div>
              ) : null}
            </SectionCard>
          ))}
        </div>
      )}
    </main>
  );
}
