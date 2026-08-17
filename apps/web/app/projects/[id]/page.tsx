import { ProjectDashboard } from "../../../components/dashboard/project-dashboard";
export default function ProjectHome({ params }: { params: { id: string } }) { return <ProjectDashboard projectId={params.id} />; }
