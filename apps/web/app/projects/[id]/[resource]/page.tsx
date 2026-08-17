import { ResourceWorkspace } from "../../../../components/resources/resource-workspace";
export default function ResourcePage({ params }: { params: { id: string; resource: string } }) { return <ResourceWorkspace projectId={params.id} resource={params.resource} />; }
