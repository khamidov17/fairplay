// Pure logic of the "Check answer regions" editor (tested in src/test/anyPaper.test.tsx).
import type { AnswerSet, Box, Region, RegionIn, SetsQuestion } from "../../../api/anyPaper";

export const MIN_SIDE = 0.02;
export const STEP = 0.005;

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const r4 = (v: number) => Math.round(v * 10000) / 10000;

/** Ordered, inside the page, at least MIN_SIDE wide/high. */
export function clampBox(b: Box, min = MIN_SIDE): Box {
  let [x0, y0, x1, y1] = [clamp01(Math.min(b[0], b[2])), clamp01(Math.min(b[1], b[3])), clamp01(Math.max(b[0], b[2])), clamp01(Math.max(b[1], b[3]))];
  if (x1 - x0 < min) {
    x1 = Math.min(1, x0 + min);
    x0 = Math.max(0, x1 - min);
  }
  if (y1 - y0 < min) {
    y1 = Math.min(1, y0 + min);
    y0 = Math.max(0, y1 - min);
  }
  return [r4(x0), r4(y0), r4(x1), r4(y1)];
}

/** Move without changing the size; stops at the page edges. */
export function moveBox(b: Box, dx: number, dy: number): Box {
  const w = b[2] - b[0];
  const h = b[3] - b[1];
  const x0 = Math.min(1 - w, Math.max(0, b[0] + dx));
  const y0 = Math.min(1 - h, Math.max(0, b[1] + dy));
  return [r4(x0), r4(y0), r4(x0 + w), r4(y0 + h)];
}

export type Handle = "nw" | "ne" | "sw" | "se";

/** Drag one corner. The opposite corner stays; the box never flips or gets too small. */
export function resizeBox(b: Box, handle: Handle, dx: number, dy: number, min = MIN_SIDE): Box {
  let [x0, y0, x1, y1] = b;
  if (handle === "nw" || handle === "sw") x0 = Math.min(clamp01(x0 + dx), x1 - min);
  else x1 = Math.max(clamp01(x1 + dx), x0 + min);
  if (handle === "nw" || handle === "ne") y0 = Math.min(clamp01(y0 + dy), y1 - min);
  else y1 = Math.max(clamp01(y1 + dy), y0 + min);
  return clampBox([x0, y0, x1, y1], min);
}

/** Box from a drag between two points (any direction). */
export function boxFromPoints(ax: number, ay: number, bx: number, by: number): Box {
  return clampBox([ax, ay, bx, by]);
}

/** Arrow keys: move (Shift: grow/shrink the bottom-right corner). */
export function nudge(b: Box, key: string, shift: boolean, step = STEP): Box | null {
  const d: Record<string, [number, number]> = {
    ArrowLeft: [-step, 0],
    ArrowRight: [step, 0],
    ArrowUp: [0, -step],
    ArrowDown: [0, step],
  };
  const v = d[key];
  if (!v) return null;
  return shift ? resizeBox(b, "se", v[0], v[1]) : moveBox(b, v[0], v[1]);
}

let seq = 0;
export function newRegionId(): string {
  seq += 1;
  return `n${Date.now().toString(36)}${seq}`;
}

export function addRegion(regions: Region[], questionId: string | null, pageId: number, box: Box): Region[] {
  return [
    ...regions,
    {
      id: newRegionId(),
      question_id: questionId,
      page_id: pageId,
      box: clampBox(box),
      confidence: 1,
      crossed_out: false,
      label: "",
      source: "teacher",
      needs_check: false,
    },
  ];
}

/** Replace one region; any teacher edit makes it a sure, teacher-made box. */
export function updateRegion(regions: Region[], id: string, patch: Partial<Region>): Region[] {
  return regions.map((r) => (r.id === id ? { ...r, ...patch, source: "teacher", confidence: 1, needs_check: false } : r));
}

export function reassign(regions: Region[], id: string, questionId: string | null): Region[] {
  return updateRegion(regions, id, { question_id: questionId });
}

export function removeRegion(regions: Region[], id: string): Region[] {
  return regions.filter((r) => r.id !== id);
}

/** "Not answered": take every (not crossed-out) box of the question away. */
export function markNotAnswered(regions: Region[], questionId: string): Region[] {
  return regions.filter((r) => r.question_id !== questionId || r.crossed_out);
}

export type QuestionState = { kind: "found" | "check" | "missing"; confidence: number | null; parts: number };

export function questionState(questionId: string, regions: Region[], lowBelow = 0.7): QuestionState {
  const live = regions.filter((r) => r.question_id === questionId && !r.crossed_out);
  if (live.length === 0) return { kind: "missing", confidence: null, parts: 0 };
  const conf = Math.min(...live.map((r) => (r.source === "teacher" ? 1 : r.confidence)));
  const check = live.some((r) => r.source !== "teacher" && (r.needs_check || r.confidence < lowBelow));
  return { kind: check ? "check" : "found", confidence: conf, parts: live.length };
}

function key(r: Region | RegionIn): string {
  return [r.question_id ?? "", r.page_id, r.box.map((v) => v.toFixed(4)).join(","), r.crossed_out ? 1 : 0].join("|");
}

/** True when the edited regions differ from the saved ones. */
export function regionsDirty(saved: Region[], edited: Region[]): boolean {
  if (saved.length !== edited.length) return true;
  const a = saved.map(key).sort();
  const b = edited.map(key).sort();
  return a.some((k, i) => k !== b[i]);
}

export function toRegionIn(regions: Region[]): RegionIn[] {
  return regions.map((r) => ({
    id: r.id,
    question_id: r.question_id,
    page_id: r.page_id,
    box: r.box,
    crossed_out: r.crossed_out,
    label: r.label,
  }));
}

export type SetFilter = "check" | "all";

export function visibleSets(sets: AnswerSet[], filter: SetFilter): AnswerSet[] {
  return filter === "all" ? sets : sets.filter((s) => s.needs_check);
}

/** Next / previous set id in the visible list (wraps); null when the list is empty. */
export function stepSet(sets: AnswerSet[], current: string | null, dir: 1 | -1): string | null {
  if (sets.length === 0) return null;
  const i = sets.findIndex((s) => s.set_id === current);
  if (i < 0) return sets[0].set_id;
  return sets[(i + dir + sets.length) % sets.length].set_id;
}

/** page_ids of the target set after moving one page into it (appended at the end). */
export function pagesAfterMove(sets: AnswerSet[], pageId: number, toSetId: string): number[] | null {
  const target = sets.find((s) => s.set_id === toSetId);
  if (!target) return null;
  const ids = target.pages.map((p) => p.page_id);
  if (ids.includes(pageId)) return null;
  return [...ids, pageId];
}

/** The question with this 1-based number (keyboard 1–9). */
export function questionByNumber(questions: SetsQuestion[], n: number): SetsQuestion | null {
  return questions.find((q) => q.index === n - 1) ?? null;
}

export type KeyAction =
  | { type: "next" }
  | { type: "prev" }
  | { type: "nextBox" }
  | { type: "prevBox" }
  | { type: "assign"; n: number }
  | { type: "delete" }
  | { type: "crossed" }
  | { type: "notAnswered" }
  | { type: "confirm" }
  | { type: "save" }
  | { type: "deselect" }
  | { type: "nudge"; key: string; shift: boolean };

/** Keyboard map of the editor (ignored while typing in a field). */
export function keyAction(e: { key: string; shiftKey?: boolean; ctrlKey?: boolean; metaKey?: boolean }): KeyAction | null {
  if (e.ctrlKey || e.metaKey) return null;
  const k = e.key;
  if (k === "j" || k === "PageDown") return { type: "next" };
  if (k === "k" || k === "PageUp") return { type: "prev" };
  if (k === "]" || k === "Tab") return e.shiftKey ? { type: "prevBox" } : { type: "nextBox" };
  if (k === "[") return { type: "prevBox" };
  if (/^[1-9]$/.test(k)) return { type: "assign", n: Number(k) };
  if (k === "Delete" || k === "Backspace") return { type: "delete" };
  if (k === "x") return { type: "crossed" };
  if (k === "n") return { type: "notAnswered" };
  if (k === "Enter") return { type: "confirm" };
  if (k === "s") return { type: "save" };
  if (k === "Escape") return { type: "deselect" };
  if (k.startsWith("Arrow")) return { type: "nudge", key: k, shift: !!e.shiftKey };
  return null;
}

/** Boxes on the pages in reading order (Tab order). */
export function regionOrder(regions: Region[], pageIds: number[]): Region[] {
  const idx = new Map(pageIds.map((p, i) => [p, i]));
  return [...regions].sort(
    (a, b) => (idx.get(a.page_id) ?? 0) - (idx.get(b.page_id) ?? 0) || a.box[1] - b.box[1] || a.box[0] - b.box[0],
  );
}

export const QUESTION_COLORS = ["#2F4BD8", "#B4570B", "#16794C", "#9C2BAF", "#B42318", "#0B7C86", "#6B5B00", "#C2185B"];

export function questionColor(index: number): string {
  return QUESTION_COLORS[((index % QUESTION_COLORS.length) + QUESTION_COLORS.length) % QUESTION_COLORS.length];
}

export const REASON_TEXT: Record<string, string> = {
  no_student: "Choose the student",
  check_student: "Check the student",
  low_confidence: "Some boxes are unsure",
  missing: "Some answers were not found",
  not_found_by_ai: "Draw the boxes by hand",
};

export function setTitle(s: AnswerSet): string {
  if (s.student) return s.student.name;
  return s.name_read ? `“${s.name_read}” (not matched)` : "Unknown student";
}
