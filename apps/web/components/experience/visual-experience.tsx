"use client";

import { useEffect, useState } from "react";
import { Pause, Play } from "lucide-react";

export function VisualExperience() {
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    setPaused(localStorage.getItem("nwe-motion-paused") === "true");
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.motion = paused || reduced ? "paused" : "active";
    window.dispatchEvent(new Event("nwe-motion-change"));
  }, [paused, reduced]);
  return <button className="motion-control" type="button" aria-label={paused || reduced ? "播放动态效果" : "暂停动态效果"} aria-pressed={paused || reduced} title={reduced ? "已遵循系统的减少动态效果设置" : "动态效果偏好会保存在本机"} onClick={() => { const next = !paused; setPaused(next); localStorage.setItem("nwe-motion-paused", String(next)); }} disabled={reduced}>
    {paused || reduced ? <Play size={13} /> : <Pause size={13} />}<span>{paused || reduced ? "动态已暂停" : "暂停动态"}</span>
  </button>;
}
