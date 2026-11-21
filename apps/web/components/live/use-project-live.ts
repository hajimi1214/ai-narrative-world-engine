"use client";

import { useEffect, useRef, useState } from "react";
import { api, apiUrl } from "../../app/lib";
import type { RunSummary } from "../../app/run-recovery";
import { createLivePoller, type LiveSyncState } from "./live-poller";

export type Trace = { id: string; stage?: string; status?: string; model?: string | null; latency_ms?: number | null; error_code?: string | null; created_at?: string | null; completed_at?: string | null; source_type?: string | null; validation_report?: { live_phase?: string; live_message?: string; generation_warning?: string } };
export type AutoRun = RunSummary & { updated_at?: string; steps?: Array<{ id: string; stage?: string; status?: string; error_code?: string; started_at?: string; completed_at?: string; usage_metrics?: { model?: string; latency_ms?: number } }> };
export type LiveSession = { id: string; label: string; model?: string | null; phase: string; message: string; preview: string; total_chars: number; status: string; started_at: string; updated_at: string; completed_at?: string | null; error_code?: string | null };

export function useProjectLive(projectId: string) {
  const [execution, setExecution] = useState<Trace[]>([]);
  const [autoRuns, setAutoRuns] = useState<AutoRun[]>([]);
  const [liveSessions, setLiveSessions] = useState<LiveSession[]>([]);
  const [sync, setSync] = useState<LiveSyncState>({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 });
  const [streamState, setStreamState] = useState<"paused" | "connecting" | "live" | "recovering">("connecting");
  const [lastStreamAt, setLastStreamAt] = useState<number | null>(null);
  const pollerRef = useRef<ReturnType<typeof createLivePoller> | null>(null);

  useEffect(() => {
    let alive = true;
    let liveRevision = 0;
    let stream: EventSource | null = null;
    setExecution([]); setAutoRuns([]); setLiveSessions([]); setLastStreamAt(null);
    setSync({ status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 });
    const poller = createLivePoller({
      onState: (state) => { if (alive) setSync(state); },
      load: async (signal) => {
        const revisionAtStart = liveRevision;
        const results = await Promise.allSettled([
          api(`/projects/${projectId}/execution-traces?limit=18`, { signal }) as Promise<Trace[]>,
          api(`/projects/${projectId}/auto-director/runs?limit=3&step_limit=24`, { signal }) as Promise<AutoRun[]>,
          api(`/projects/${projectId}/ai-live`, { signal }) as Promise<LiveSession[]>,
        ]);
        if (!alive || signal.aborted) return;
        if (results[0].status === "fulfilled" && Array.isArray(results[0].value)) setExecution(results[0].value);
        if (results[1].status === "fulfilled" && Array.isArray(results[1].value)) setAutoRuns(results[1].value);
        // A newer SSE snapshot must not be replaced by a slower HTTP response.
        if (results[2].status === "fulfilled" && Array.isArray(results[2].value) && liveRevision === revisionAtStart) setLiveSessions(results[2].value);
        if (results.some((result) => result.status === "rejected" || !Array.isArray(result.value))) throw new Error("LIVE_SYNC_INCOMPLETE");
      },
    });
    pollerRef.current = poller;
    const closeStream = () => { stream?.close(); stream = null; };
    const connectStream = () => {
      if (stream || !alive || document.hidden) return;
      if (typeof EventSource === "undefined") { setStreamState("recovering"); return; }
      setStreamState("connecting");
      let connection: EventSource;
      try { connection = new EventSource(`${apiUrl}/projects/${projectId}/ai-live/stream`); }
      catch { setStreamState("recovering"); return; }
      stream = connection;
      connection.onopen = () => { if (alive && stream === connection) setStreamState("live"); };
      connection.onerror = () => { if (alive && stream === connection) setStreamState("recovering"); };
      connection.addEventListener("snapshot", (event: Event) => {
        if (!alive || document.hidden || stream !== connection) return;
        try {
          const sessions = JSON.parse((event as MessageEvent).data).sessions;
          if (!Array.isArray(sessions)) throw new Error("INVALID_LIVE_SNAPSHOT");
          liveRevision += 1;
          setLiveSessions(sessions);
          setLastStreamAt(Date.now());
          setStreamState("live");
        } catch { setStreamState("recovering"); }
      });
    };
    const visibilityChanged = () => {
      poller.setPaused(document.hidden);
      if (document.hidden) { closeStream(); setStreamState("paused"); }
      else connectStream();
    };
    document.addEventListener("visibilitychange", visibilityChanged);
    visibilityChanged();
    return () => {
      alive = false;
      poller.dispose();
      if (pollerRef.current === poller) pollerRef.current = null;
      document.removeEventListener("visibilitychange", visibilityChanged);
      closeStream();
    };
  }, [projectId]);

  return { execution, autoRuns, liveSessions, sync, streamState, lastStreamAt, refresh: () => pollerRef.current?.refresh() };
}
