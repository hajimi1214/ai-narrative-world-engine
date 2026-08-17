import { RetconWorkspace } from "../../../../components/retcon/retcon-workspace";
export default function RetconPage({ params }: { params:{id:string} }) { return <RetconWorkspace projectId={params.id} />; }
