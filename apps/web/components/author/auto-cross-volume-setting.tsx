"use client";

import { useEffect, useState } from "react";
import { ArrowRightLeft, CircleCheck, LoaderCircle } from "lucide-react";
import { api, ApiError } from "../../app/lib";
import { ErrorState, SectionCard } from "../ui/primitives";

type RunPolicy = { book_policy?: { auto_cross_volume?: boolean }; settings?: { auto_cross_volume?: boolean } };

export function AutoCrossVolumeSetting({ projectId }: { projectId: string }) {
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void api(`/projects/${projectId}/author-guided-volume/runs`, { signal: controller.signal })
      .then((value: unknown) => {
        const runs = Array.isArray(value) ? value as RunPolicy[] : [];
        const current = runs[0];
        setEnabled(Boolean(current?.book_policy?.auto_cross_volume ?? current?.settings?.auto_cross_volume));
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof ApiError ? reason.message : "无法读取作品自动化设置");
      })
      .finally(() => { if (!controller.signal.aborted) setLoaded(true); });
    return () => controller.abort();
  }, [projectId]);

  async function update(value: boolean) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api(`/projects/${projectId}/book-contract/automation`, {
        method: "PATCH",
        body: JSON.stringify({ auto_cross_volume: value }),
      }) as { auto_cross_volume?: boolean };
      setEnabled(Boolean(result.auto_cross_volume));
      setMessage(value ? "自动跨卷已开启。当前卷通过检查并保存快照后会继续。" : "自动跨卷已关闭。当前卷完成后会停下来等待作者。" );
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "保存作品设置失败");
    } finally {
      setBusy(false);
    }
  }

  return <SectionCard title="自动跨卷" aside={enabled ? <CircleCheck size={16} aria-label="已开启" /> : <ArrowRightLeft size={16} aria-label="按作品设置" />}>
    <div className="form-grid">
      <label className="checkbox-field">
        <input type="checkbox" checked={enabled} disabled={!loaded || busy || Boolean(error)} onChange={(event) => void update(event.target.checked)} />
        <span>{busy ? <><LoaderCircle size={14} />正在保存</> : enabled ? "当前作品已启用自动跨卷" : "当前作品关闭自动跨卷"}</span>
      </label>
      <p className="muted">开启后，只有当前卷所有章节通过审核、卷目标满足并成功保存连续性快照，才会转到下一卷。预算耗尽或下一卷缺少有效规划时会暂停。</p>
    </div>
    {error && <ErrorState message={error} retry={() => window.location.reload()} />}
    {message && <p className="success-state" role="status">{message}</p>}
  </SectionCard>;
}
