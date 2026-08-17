import type { ReactNode } from "react";
import { ProjectShell } from "../../../components/layout/project-shell";
export default function ProjectLayout({ children, params }: { children: ReactNode; params: { id: string } }) { return <ProjectShell projectId={params.id}>{children}</ProjectShell>; }
