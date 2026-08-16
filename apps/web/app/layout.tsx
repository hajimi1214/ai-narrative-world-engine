import type { ReactNode } from "react";
import "./globals.css";

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body><div className="shell"><header className="topbar"><a className="brand" href="/">AI Narrative World Engine</a><span className="muted">Novel World Core</span></header>{children}</div></body></html>;
}
