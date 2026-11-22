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

export type RequiredEventEvidenceRow = {
  requirement_type: string;
  requirement_ref: string | null;
  event_ref: string | null;
  label: string;
  review: MandatoryEventReview | null;
};

function requirementItems(task: any, field: "must_events" | "foreshadow_payoff") {
  const contracted = task?.event_contracts?.[field];
  return Array.isArray(contracted) && contracted.length
    ? contracted
    : Array.isArray(task?.[field]) ? task[field] : [];
}

function eventSpec(value: any, requirementType: string) {
  const eventRef = typeof value === "object" && value
    ? String(value.event_ref || value.canonical_key || value.key || value.foreshadow_ref || value.id || "")
    : "";
  const label = typeof value === "object" && value
    ? String(value.label || value.name || value.text || value.title || value.foreshadow_ref || value.event_ref || "").trim()
    : String(value || "").trim();
  return {
    requirement_type: requirementType,
    requirement_ref: eventRef ? `${requirementType}:${eventRef}` : null,
    event_ref: eventRef || null,
    label,
  };
}

export function requiredEventEvidenceRows(task: any, reviews: MandatoryEventReview[]) {
  const requirements = [
    ...requirementItems(task, "must_events").map((item: any) => eventSpec(item, "must_events")),
    ...requirementItems(task, "foreshadow_payoff").map((item: any) => eventSpec(item, "foreshadow_payoff")),
  ];
  const unusedReviewIndexes = new Set(reviews.map((_review, index) => index));
  const rows: RequiredEventEvidenceRow[] = requirements.map((requirement) => {
    const matchIndex = reviews.findIndex((review, index) => {
      if (!unusedReviewIndexes.has(index)) return false;
      if (review.requirement_type && review.requirement_type !== requirement.requirement_type) return false;
      if (requirement.requirement_ref && review.requirement_ref === requirement.requirement_ref) return true;
      if (requirement.event_ref && review.event_ref === requirement.event_ref) return true;
      return Boolean(requirement.label && review.label === requirement.label);
    });
    const review = matchIndex < 0 ? null : reviews[matchIndex];
    if (matchIndex >= 0) unusedReviewIndexes.delete(matchIndex);
    return { ...requirement, review };
  });
  const unmatchedReviews = Array.from(unusedReviewIndexes).map((index) => reviews[index]);
  return { rows, unmatchedReviews };
}

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
