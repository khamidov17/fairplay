// Any-paper answer finding (pages without FairPlay codes): student grouping + answer
// regions found by the AI, checked by the teacher. Server: app/scan/any_paper.py.
import { request } from "./client";

const enc = encodeURIComponent;

/** [x0, y0, x1, y1], fractions of the page width/height, origin top-left. */
export type Box = [number, number, number, number];

export interface Region {
  id: string;
  question_id: string | null;
  page_id: number;
  box: Box;
  confidence: number;
  crossed_out: boolean;
  label: string;
  source: "ai" | "teacher" | "fallback";
  needs_check: boolean;
}

export interface SetPage {
  page_id: number;
  seq: number;
  preview: string | null;
  image: string | null;
  /** OpenCV candidate blocks the AI chose from */
  candidates: Box[];
}

export interface MatchCandidate {
  student_id: number;
  name: string;
  username: string;
  score: number;
}

export type SetReason = "no_student" | "check_student" | "low_confidence" | "missing" | "not_found_by_ai";

export interface AnswerSet {
  set_id: string;
  batch_id: string;
  student_id: number | null;
  student: { id: number; username: string; name: string } | null;
  name_read: string;
  id_read: string;
  match: { confidence: number; candidates: MatchCandidate[]; reasons: string[]; teacher: boolean };
  status: "needs_check" | "ok" | "confirmed";
  needs_check: boolean;
  reasons: SetReason[];
  pages: SetPage[];
  regions: Region[];
  missing: string[];
  segment: { prompt_version?: string; source?: string; reasons?: string[]; confidence?: number };
  updated_at: string | null;
}

export interface SetsQuestion {
  question_id: string;
  index: number;
  prompt: string;
  type: string;
  points: number;
}

export interface AnswerSets {
  exam_id: string;
  ai_enabled: boolean;
  low_confidence_below: number;
  questions: SetsQuestion[];
  students: { id: number; username: string; name: string }[];
  sets: AnswerSet[];
  needs_check: number;
  loose_pages: { page_id: number; seq: number; preview: string | null; batch_id: string }[];
  /** set after a change */
  queued?: number;
  set?: AnswerSet | null;
}

export interface RegionIn {
  id?: string;
  question_id: string | null;
  page_id: number;
  box: Box;
  crossed_out?: boolean;
  label?: string;
}

export interface SetUpdate {
  student_id?: number | null;
  page_ids?: number[];
  regions?: RegionIn[];
  confirm?: boolean;
}

export const anyPaperApi = {
  sets: (examId: string) => request<AnswerSets>("teacher", "GET", `/api/exams/${enc(examId)}/scans/sets`),
  update: (examId: string, setId: string, body: SetUpdate) =>
    request<AnswerSets>("teacher", "PUT", `/api/exams/${enc(examId)}/scans/sets/${enc(setId)}`, body),
  create: (examId: string, pageIds: number[], studentId?: number | null) =>
    request<AnswerSets>("teacher", "POST", `/api/exams/${enc(examId)}/scans/sets`, {
      page_ids: pageIds,
      ...(studentId ? { student_id: studentId } : {}),
    }),
  resegment: (examId: string, setId: string) =>
    request<AnswerSets>("teacher", "POST", `/api/exams/${enc(examId)}/scans/sets/${enc(setId)}/resegment`),
};
