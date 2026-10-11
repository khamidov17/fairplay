// Pure helpers for the AI grading queue (unit-tested in src/test/scan.test.tsx).
import type { AiDraft, PaperGrade, QueueGroup, QueueItem, ScanQueue } from "../../../api/scan";

/** review = not graded yet (same as "ungraded"); flagged = AI says needs review;
 *  low = AI confidence below the threshold. */
export type QueueFilter = "review" | "ungraded" | "flagged" | "low" | "all" | "approved";

export const LETTERS = "ABCDEFGH";

export const itemKey = (it: Pick<QueueItem, "attempt_id" | "question_id">) => `${it.attempt_id}:${it.question_id}`;

/** Items of one question for a filter. "review" = needs_review (low confidence first). */
export function itemsFor(queue: ScanQueue, qid: string, filter: QueueFilter): QueueItem[] {
  const list = queue.items.filter((i) => i.question_id === qid);
  if (filter === "approved") return list.filter((i) => i.status === "approved");
  const open = list.filter((i) => i.status === "needs_review");
  if (filter === "flagged") return open.filter(isFlagged);
  if (filter === "low") return open.filter((i) => i.low_confidence && i.draft?.source !== "fallback");
  if (filter === "review" || filter === "ungraded") {
    return open.sort((a, b) => Number(b.low_confidence) - Number(a.low_confidence));
  }
  return list;
}

/** The AI flagged this answer for a closer look (or there is no usable suggestion). */
export function isFlagged(i: QueueItem): boolean {
  if (i.status !== "needs_review") return false;
  if (!i.draft) return true;
  if (i.draft.needs_review !== undefined) return i.draft.needs_review;
  return i.draft.source === "fallback" || i.low_confidence;
}

/** Groups (≥ 2 similar drafts still to review) for one question, biggest first. */
export function groupsFor(queue: ScanQueue, qid: string): QueueGroup[] {
  const open = new Set(queue.items.filter((i) => i.status === "needs_review").map(itemKey));
  return queue.groups
    .filter((g) => g.question_id === qid)
    .map((g) => {
      const items = g.items.filter((i) => open.has(itemKey(i)));
      return { ...g, items, count: items.length };
    })
    .filter((g) => g.count >= 2)
    .sort((a, b) => b.count - a.count);
}

/** Next/previous index, clamped (J/K). */
export function stepIndex(length: number, current: number, dir: 1 | -1): number {
  if (length === 0) return 0;
  return Math.min(length - 1, Math.max(0, current + dir));
}

export type KeyAction = "approve" | "edit" | "regrade" | "next" | "prev";

/** Keyboard shortcuts; ignored while typing in a field or with modifier keys. */
export function keyAction(ev: { key: string; target?: EventTarget | null; metaKey?: boolean; ctrlKey?: boolean; altKey?: boolean }): KeyAction | null {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return null;
  const t = ev.target as HTMLElement | null;
  const tag = t && typeof t.tagName === "string" ? t.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) return null;
  if (ev.key === "Enter" && (tag === "button" || tag === "a")) return null; // let the focused control act
  switch (ev.key.toLowerCase()) {
    case "a":
    case "enter":
      return "approve";
    case "e":
      return "edit";
    case "r":
      return "regrade";
    case "j":
    case "arrowdown":
    case "arrowright":
      return "next";
    case "k":
    case "arrowup":
    case "arrowleft":
      return "prev";
    default:
      return null;
  }
}

/** Number keys set the points (0–9); ignored while typing or with modifiers. */
export function digitKey(ev: { key: string; target?: EventTarget | null; metaKey?: boolean; ctrlKey?: boolean; altKey?: boolean }): number | null {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return null;
  const t = ev.target as HTMLElement | null;
  const tag = t && typeof t.tagName === "string" ? t.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) return null;
  return /^[0-9]$/.test(ev.key) ? Number(ev.key) : null;
}

/** Queue with one item approved (local update after the server confirmed). */
export function withGrade(queue: ScanQueue, aid: string, qid: string, grade: PaperGrade): ScanQueue {
  return recount({
    ...queue,
    items: queue.items.map((i) =>
      i.attempt_id === aid && i.question_id === qid ? { ...i, grade, status: "approved" } : i,
    ),
  });
}

/** Queue with a new AI draft for one item. */
export function withDraft(queue: ScanQueue, aid: string, qid: string, draft: AiDraft): ScanQueue {
  return recount({
    ...queue,
    items: queue.items.map((i) =>
      i.attempt_id === aid && i.question_id === qid ? { ...i, draft, low_confidence: draft.low_confidence } : i,
    ),
  });
}

/** Recompute per-question counters from the items. */
export function recount(queue: ScanQueue): ScanQueue {
  return {
    ...queue,
    questions: queue.questions.map((q) => {
      const items = queue.items.filter((i) => i.question_id === q.question_id);
      return {
        ...q,
        total: items.length,
        to_review: items.filter((i) => i.status === "needs_review").length,
        approved: items.filter((i) => i.status === "approved").length,
        auto: items.filter((i) => i.status === "auto").length,
        low_confidence: items.filter((i) => i.status === "needs_review" && i.low_confidence).length,
      };
    }),
  };
}

/** First question that still has answers to review (else the first question). */
export function firstOpenQuestion(queue: ScanQueue): string | null {
  const open = queue.questions.find((q) => q.to_review > 0);
  return (open ?? queue.questions[0])?.question_id ?? null;
}

export function confidenceText(d: AiDraft | null): string {
  if (!d) return "No draft yet";
  if (d.source === "fallback") return "AI off — grade by hand";
  if (d.source === "blank") return "Looks blank";
  if (d.source === "bubbles") return "Bubble read";
  return `${Math.round(d.confidence * 100)}% sure`;
}

/** What the scanner read from an MCQ answer box. */
export function bubbleText(it: Pick<QueueItem, "marks" | "mcq">): string | null {
  if (!it.marks) return null;
  if (it.marks === "one" && it.mcq != null) return `Filled: ${LETTERS[it.mcq] ?? "?"}`;
  if (it.marks === "none") return "No bubble filled";
  if (it.marks === "multiple") return "More than one bubble filled";
  return `Faint mark${it.mcq != null ? ` (looks like ${LETTERS[it.mcq] ?? "?"})` : ""}`;
}

/** Points shown for an item: the grade, else the automatic MCQ score, else the AI draft. */
export function shownPoints(it: QueueItem): { value: number | null; kind: "grade" | "auto" | "draft" | "none" } {
  if (it.grade) return { value: it.grade.points, kind: "grade" };
  if (it.status === "auto") return { value: it.auto_points, kind: "auto" };
  if (it.draft) return { value: it.draft.points, kind: "draft" };
  return { value: null, kind: "none" };
}

/** Validate an edited score; returns an error message or null. */
export function editError(points: string, max: number): string | null {
  if (points.trim() === "") return "Enter the points.";
  const n = Number(points);
  if (!Number.isFinite(n)) return "Points must be a number.";
  if (n < 0 || n > max) return `Points must be between 0 and ${max}.`;
  return null;
}
