import { CharacterDetail } from "../../../../../components/characters/character-detail";

export default function ProjectCharacterPage({ params }: { params: { id: string; characterId: string } }) {
  return <CharacterDetail characterId={params.characterId} />;
}
