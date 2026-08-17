import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body><Providers>{children}</Providers></body></html>;
}
