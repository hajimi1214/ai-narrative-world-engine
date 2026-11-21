import { mapFindingSpan } from "./finding-span";

export type MandatoryEventReview = {
  requirement_ref?: string;
  requirement_type?: string;
  event_ref?: string;
  label?: string;
  confirmed?: boolean;
  start_offset?: number | null;
  end_offset?: number | null;
  excerpt?: string | null;
  reason?: string | null;
};

export function assessEventEvidence(
  draftContent: string | null | undefined,
  review: MandatoryEventReview,
  reviewMatchesDraft: boolean,
) {
  if (!reviewMatchesDraft) return { status: "STALE" as const, span: null };
  const span = mapFindingSpan(draftContent, review.start_offset, review.end_offset, review.excerpt);
  if (!review.confirmed) return { status: "NOT_CONFIRMED" as const, span };
  return { status: span ? "CONFIRMED" as const : "INVALID_EVIDENCE" as const, span };
}
