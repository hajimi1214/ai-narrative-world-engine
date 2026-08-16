import type { ReactNode } from "react";
const items = [["人物", "characters"], ["世界实体", "world-entities"], ["Canon", "canon"], ["Story Thread", "story-threads"], ["Writing Bible", "writing-bibles"], ["Anti-AI Bible", "anti-ai-bibles"]];
export default function ProjectLayout({ children, params }: { children: ReactNode; params: { id: string } }) { return <><nav className="project-nav"><a href={`/projects/${params.id}`}>概览</a>{items.map(([label, path]) => <a href={`/projects/${params.id}/${path}`} key={path}>{label}</a>)}</nav>{children}</>; }
