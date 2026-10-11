// Pure logic of the student homework upload page (tested in src/test/homework.test.tsx).
import type { StudentHomework, StudentHomeworkState } from "../../../api/homework";

export const MAX_PAGES = 30;
export const MAX_TOTAL_BYTES = 80 * 1024 * 1024;
const ACCEPTED = /^(image\/(jpeg|png|webp|heic|heif)|application\/pdf)$/i;
const ACCEPTED_EXT = /\.(jpe?g|png|webp|heic|heif|pdf)$/i;

export interface PageItem {
  id: string;
  file: File;
  /** object URL for the thumbnail ("" for PDFs) */
  url: string;
}

let counter = 0;

export function isAcceptedFile(f: File): boolean {
  return ACCEPTED.test(f.type || "") || ACCEPTED_EXT.test(f.name || "");
}

/** Append new photos in the order the camera / picker returned them. Rejected files are named. */
export function addPages(
  list: PageItem[],
  files: File[],
  makeUrl: (f: File) => string = (f) => (f.type.startsWith("image/") ? URL.createObjectURL(f) : ""),
): { pages: PageItem[]; rejected: string[] } {
  const rejected: string[] = [];
  const next = [...list];
  for (const f of files) {
    if (!isAcceptedFile(f)) {
      rejected.push(f.name || "file");
      continue;
    }
    next.push({ id: `p${Date.now().toString(36)}_${++counter}`, file: f, url: makeUrl(f) });
  }
  return { pages: next, rejected };
}

export function movePage(list: PageItem[], index: number, delta: number): PageItem[] {
  const to = index + delta;
  if (to < 0 || to >= list.length) return list;
  const next = [...list];
  const [it] = next.splice(index, 1);
  next.splice(to, 0, it);
  return next;
}

export function removePage(list: PageItem[], id: string): PageItem[] {
  return list.filter((p) => p.id !== id);
}

/** Why the pages cannot be sent yet (null = ok). */
export function validatePages(list: PageItem[]): string | null {
  if (list.length === 0) return "Add a photo of each page first.";
  if (list.length > MAX_PAGES) return `Send at most ${MAX_PAGES} pages.`;
  const total = list.reduce((s, p) => s + (p.file.size || 0), 0);
  if (total > MAX_TOTAL_BYTES) return "The photos are too large together (80 MB). Remove some or take them again.";
  return null;
}

const DAY = 86_400_000;

/** "Due today, 17:00" / "Due tomorrow, 09:00" / "Due in 3 days" / "Overdue by 2 days" / "Due Oct 3". */
export function dueLabel(dueIso: string, now: Date = new Date()): string {
  const due = new Date(dueIso);
  if (Number.isNaN(due.getTime())) return "";
  const time = due.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const diff = due.getTime() - now.getTime();
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (diff < 0) {
    const days = Math.floor(-diff / DAY);
    if (days === 0) return `Was due ${sameDay(due, now) ? "today" : "yesterday"}, ${time}`;
    return `Overdue by ${days} day${days === 1 ? "" : "s"}`;
  }
  if (sameDay(due, now)) return `Due today, ${time}`;
  const tomorrow = new Date(now.getTime() + DAY);
  if (sameDay(due, tomorrow)) return `Due tomorrow, ${time}`;
  const days = Math.ceil(diff / DAY);
  if (days <= 6) return `Due in ${days} days`;
  return `Due ${due.toLocaleDateString([], { month: "short", day: "numeric" })}`;
}

export const STATE_LABEL: Record<StudentHomeworkState, string> = {
  not_submitted: "To do",
  processing: "Reading your pages",
  failed: "Upload again",
  handed_in: "Handed in",
  feedback: "Feedback ready",
};

export function stateTone(h: Pick<StudentHomework, "state" | "overdue" | "feedback_new">): string {
  if (h.state === "feedback") return h.feedback_new ? "info" : "success";
  if (h.state === "failed" || h.overdue) return "danger";
  if (h.state === "not_submitted") return "warn";
  return "";
}

/** Label of the main button: first hand-in, resubmission, or late. */
export function submitLabel(h: Pick<StudentHomework, "state" | "past_due">, pages: number): string {
  const n = pages === 1 ? "1 page" : `${pages} pages`;
  if (h.state === "handed_in" || h.state === "processing") return `Hand in again (${n})`;
  if (h.past_due) return `Hand in late (${n})`;
  return `Hand in ${n}`;
}
