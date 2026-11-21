export type DraftChange = { kind: "same" | "removed" | "added"; text: string };

/** Produce a compact line-level diff; cap quadratic work for very long chapters. */
export function compareDraftLines(oldText: string, newText: string, maxCells = 180_000): DraftChange[] {
  const oldLines = oldText.replace(/\r\n?/g, "\n").split("\n");
  const newLines = newText.replace(/\r\n?/g, "\n").split("\n");
  if (oldLines.length * newLines.length > maxCells) return [
    { kind: "removed", text: `旧版共 ${oldLines.length} 行` },
    { kind: "added", text: `当前工作稿共 ${newLines.length} 行；章节过长，按行数摘要对比` },
  ];
  const columns = newLines.length + 1;
  const table = new Uint32Array((oldLines.length + 1) * columns);
  for (let i = oldLines.length - 1; i >= 0; i -= 1) for (let j = newLines.length - 1; j >= 0; j -= 1) {
    const at = i * columns + j;
    table[at] = oldLines[i] === newLines[j] ? table[(i + 1) * columns + j + 1] + 1 : Math.max(table[(i + 1) * columns + j], table[i * columns + j + 1]);
  }
  const changes: DraftChange[] = [];
  let i = 0;
  let j = 0;
  while (i < oldLines.length || j < newLines.length) {
    let kind: DraftChange["kind"];
    let text: string;
    if (i < oldLines.length && j < newLines.length && oldLines[i] === newLines[j]) { kind = "same"; text = oldLines[i]; i += 1; j += 1; }
    else if (i < oldLines.length && (j >= newLines.length || table[(i + 1) * columns + j] >= table[i * columns + j + 1])) { kind = "removed"; text = oldLines[i++]; }
    else { kind = "added"; text = newLines[j++]; }
    const previous = changes[changes.length - 1];
    if (previous?.kind === kind) previous.text += `\n${text}`;
    else changes.push({ kind, text });
  }
  return changes;
}
