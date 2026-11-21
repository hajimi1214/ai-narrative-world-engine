/** Map Python/code-point offsets from quality findings onto JavaScript's UTF-16 editor offsets. */
export function mapFindingSpan(text: string | null | undefined, start: number | null | undefined, end: number | null | undefined, excerpt: string | null | undefined): { start: number; end: number } | null {
  // Draft and assessment requests finish independently. Until the draft arrives,
  // findings may render with no text; treat that as non-actionable rather than
  // letting the Unicode walk throw during React rendering.
  if (typeof text !== "string" || !Number.isInteger(start) || !Number.isInteger(end) || start! < 0 || start! >= end! || typeof excerpt !== "string" || !excerpt) return null;
  const toUtf16Offset = (codePointOffset: number): number | null => {
    let points = 0;
    let units = 0;
    for (const character of text) {
      if (points === codePointOffset) return units;
      units += character.length;
      points += 1;
    }
    return points === codePointOffset ? units : null;
  };
  const mappedStart = toUtf16Offset(start!);
  const mappedEnd = toUtf16Offset(end!);
  if (mappedStart === null || mappedEnd === null || mappedEnd <= mappedStart || text.slice(mappedStart, mappedEnd) !== excerpt) return null;
  return { start: mappedStart, end: mappedEnd };
}
