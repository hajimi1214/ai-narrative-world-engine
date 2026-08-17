import { RetconPlanView } from "../../../../../../components/retcon/retcon-workspace";
export default function RetconPlanPage({ params }: { params:{id:string; planId:string} }) { return <RetconPlanView projectId={params.id} planId={params.planId} />; }
