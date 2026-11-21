export type LiveSyncState = {
  status: "paused" | "syncing" | "ready" | "offline";
  lastUpdatedAt: number | null;
  retryAt: number | null;
  failures: number;
};

type PollerOptions = {
  load: (signal: AbortSignal) => Promise<void>;
  onState: (state: LiveSyncState) => void;
  intervalMs?: number;
  maxBackoffMs?: number;
  timeoutMs?: number;
};

/** One request batch at a time, including after visibility changes and retries. */
export function createLivePoller({ load, onState, intervalMs = 3000, maxBackoffMs = 30000, timeoutMs = 15000 }: PollerOptions) {
  let disposed = false;
  let paused = true;
  let inFlight = false;
  let refreshPending = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let state: LiveSyncState = { status: "paused", lastUpdatedAt: null, retryAt: null, failures: 0 };

  const publish = (next: Partial<LiveSyncState>) => {
    state = { ...state, ...next };
    if (!disposed) onState(state);
  };
  const cancelTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
  const schedule = (delay: number) => {
    cancelTimer();
    if (!disposed && !paused) timer = setTimeout(() => { timer = undefined; void run(); }, delay);
  };
  const run = async () => {
    if (disposed || paused) return;
    if (inFlight) { refreshPending = true; return; }
    inFlight = true;
    refreshPending = false;
    controller = new AbortController();
    const active = controller;
    const timeout = setTimeout(() => active.abort("timeout"), timeoutMs);
    publish({ status: "syncing", retryAt: null });
    let delay = intervalMs;
    try {
      await load(active.signal);
      if (active.signal.aborted) throw new Error("LIVE_REQUEST_ABORTED");
      if (!disposed && !paused) publish({ status: "ready", lastUpdatedAt: Date.now(), failures: 0 });
    } catch {
      if (!disposed && !paused && active.signal.reason !== "hidden") {
        const failures = state.failures + 1;
        delay = Math.min(maxBackoffMs, intervalMs * 2 ** Math.min(failures, 6));
        publish({ status: "offline", failures, retryAt: Date.now() + delay });
      }
    } finally {
      clearTimeout(timeout);
      inFlight = false;
      controller = undefined;
      if (!disposed && !paused) schedule(refreshPending ? 0 : delay);
    }
  };

  return {
    setPaused(value: boolean) {
      if (disposed || paused === value) return;
      paused = value;
      cancelTimer();
      if (paused) {
        refreshPending = false;
        controller?.abort("hidden");
        publish({ status: "paused", retryAt: null });
      } else {
        void run();
      }
    },
    refresh() { cancelTimer(); if (!paused && !disposed) void run(); },
    dispose() {
      disposed = true;
      paused = true;
      refreshPending = false;
      cancelTimer();
      controller?.abort("disposed");
    },
  };
}
