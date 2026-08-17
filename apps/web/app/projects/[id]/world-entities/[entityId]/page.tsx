import { EntityDetail } from "../../../../../components/world/entity-detail";

export default function ProjectEntityPage({ params }: { params: { id: string; entityId: string } }) {
  return <EntityDetail entityId={params.entityId} />;
}
