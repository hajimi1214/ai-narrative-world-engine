import type { ReactNode } from "react";
import { AlertCircle, Inbox } from "lucide-react";

export function PageHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) { return <header className="page-header"><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{action}</header>; }
export function SectionCard({ title, children, action, aside, description }: { title?: string; children: ReactNode; action?: ReactNode; aside?: ReactNode; description?: string }) { return <section className="section-card">{title && <div className="section-heading"><div><h2>{title}</h2>{description && <p className="muted">{description}</p>}</div>{action || aside}</div>}{children}</section>; }
export function StatusBadge({ value }: { value?: string | null }) { const normalized = (value || "未知").toLowerCase(); return <span className={`status-badge status-${normalized}`}>{value || "未知"}</span>; }
export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) { return <div className="empty-state"><Inbox size={28} /><strong>{title}</strong><p>{description}</p>{action}</div>; }
export function ErrorState({ message, retry }: { message: string; retry?: () => void }) { return <div className="error-state"><AlertCircle size={20} /><span>{message}</span>{retry && <button className="button secondary" onClick={retry}>重新加载</button>}</div>; }
export function LoadingState() { return <div className="loading-state"><span className="loading-dot" />正在加载世界数据…<div className="loading-skeleton-lines" aria-hidden="true"><span /><span /><span /></div></div>; }
export function DeveloperData({ value, label = "开发者数据" }: { value: unknown; label?: string }) { return <details className="developer-data"><summary>{label}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>; }
