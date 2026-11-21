import type { ReactNode } from "react";
import "./globals.css";
import "./visual-refresh.css";
import { Providers } from "./providers";
import { VisualExperience } from "../components/experience/visual-experience";

export const metadata = { title: "Narrative · 长篇小说工作台", description: "从一句灵感到整本长篇，按卷规划、创作、改稿与审核。" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body><Providers>{children}<VisualExperience /></Providers></body></html>;
}
