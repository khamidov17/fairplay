// Classes (Google-Classroom style): types + API calls. Own module so the shared
// types.ts / client.ts stay untouched by this feature (CONTRACT.md "Classes").
import { ApiError, NetworkError, getToken, request } from "./client";
import type { Gradebook } from "./types";

const enc = encodeURIComponent;

function gbQuery(course: string, classId: number): string {
  return `?class_id=${classId}${course ? `&course=${enc(course)}` : ""}`;
}

export interface ClassSummary {
  id: number;
  name: string;
  section: string;
  term: string;
  description: string;
  join_code: string;
  join_enabled: boolean;
  archived: boolean;
  created_at: string;
  member_count: number;
  exam_count: number;
}

export interface ClassStudent {
  id: number;
  username: string;
  name: string;
  email: string | null;
  joined_at?: string;
  /** teacher = added by username/email/CSV, code = joined with the class code, lms = imported */
  added_by?: "teacher" | "code" | "lms";
}

export interface ClassExam {
  id: string;
  title: string;
  status: "draft" | "live" | "closed";
  course: string;
  delivery: "computer" | "paper" | "website";
  scheduled_at: string | null;
  created_at: string;
}

export interface ClassDetail extends ClassSummary {
  students: ClassStudent[];
  exams: ClassExam[];
}

export interface ClassInput {
  name: string;
  section?: string;
  term?: string;
  description?: string;
}

export type ClassPatch = Partial<ClassInput> & { join_enabled?: boolean; archived?: boolean };

export interface AddMembersResult {
  added: ClassStudent[];
  already: ClassStudent[];
  /** Entries (or CSV rows) that matched no student account. */
  not_found: string[];
  class: ClassSummary;
}

/** Who may start an exam: explicit roster > assigned classes > linked LMS course > open. */
export type ExamAudience = "roster" | "classes" | "course" | "open";

export interface ExamClasses {
  exam_id: string;
  classes: { id: number; name: string; section: string; term: string; archived: boolean; member_count: number }[];
  /** Students in the union of the assigned classes. */
  student_count: number;
  roster_override: boolean;
  audience: ExamAudience;
}

export interface StudentClassExam {
  id: string;
  title: string;
  course: string;
  status: "draft" | "live" | "closed";
  scheduled_at: string | null;
  duration_min: number;
  delivery: string;
  attempt: { attempt_id: string; status: "in_progress" | "submitted" } | null;
}

export interface StudentClass {
  id: number;
  name: string;
  section: string;
  term: string;
  description: string;
  teacher_name: string;
  joined_at: string;
  exams: StudentClassExam[];
}

export interface JoinResult {
  id: number;
  name: string;
  section: string;
  term: string;
  teacher_name: string;
  already_member: boolean;
}

export const classesApi = {
  list: (archived = false) =>
    request<ClassSummary[]>("teacher", "GET", `/api/classes${archived ? "?archived=true" : ""}`),
  create: (input: ClassInput) => request<ClassSummary>("teacher", "POST", "/api/classes", input),
  get: (id: number) => request<ClassDetail>("teacher", "GET", `/api/classes/${id}`),
  update: (id: number, patch: ClassPatch) => request<ClassSummary>("teacher", "PATCH", `/api/classes/${id}`, patch),
  regenerateCode: (id: number) => request<ClassSummary>("teacher", "POST", `/api/classes/${id}/join-code`),
  addMembers: (id: number, text: string) =>
    request<AddMembersResult>("teacher", "POST", `/api/classes/${id}/members`, { text }),
  addCsv: (id: number, csv: string) =>
    request<AddMembersResult>("teacher", "POST", `/api/classes/${id}/members/csv`, { csv }),
  importLms: (id: number, courseLinkId: number) =>
    request<AddMembersResult>("teacher", "POST", `/api/classes/${id}/members/lms`, { course_link_id: courseLinkId }),
  removeMember: (id: number, userId: number) =>
    request<void>("teacher", "DELETE", `/api/classes/${id}/members/${userId}`),
  examClasses: (examId: string) => request<ExamClasses>("teacher", "GET", `/api/exams/${enc(examId)}/classes`),
  setExamClasses: (examId: string, classIds: number[]) =>
    request<ExamClasses>("teacher", "PUT", `/api/exams/${enc(examId)}/classes`, { class_ids: classIds }),
  /** Gradebook limited to one class: its students and the exams assigned to it. */
  gradebook: (course: string, classId: number) =>
    request<Gradebook>("teacher", "GET", `/api/gradebook${gbQuery(course, classId)}`),
  async gradebookCsv(course: string, classId: number): Promise<Blob> {
    const token = getToken("teacher");
    let res: Response;
    try {
      res = await fetch(`/api/gradebook.csv${gbQuery(course, classId)}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
    } catch {
      throw new NetworkError();
    }
    if (!res.ok) throw new ApiError(res.status, res.statusText || `Error ${res.status}`);
    return res.blob();
  },
  // student
  mine:() => request<StudentClass[]>("student", "GET", "/api/student/classes"),
  join: (code: string) => request<JoinResult>("student", "POST", "/api/student/classes/join", { code }),
};

// The student exam list names the student's classes for each exam (GET /api/student/exams).
declare module "./types" {
  interface StudentExamSummary {
    /** Names of the student's classes this exam is assigned to ([] when none). */
    class_names?: string[];
  }
}

/** "Calculus I · MATH 101 · Fall 2026" */
export function classLabel(c: { name: string; section?: string; term?: string }): string {
  return [c.name, c.section, c.term].filter(Boolean).join(" · ");
}

/** Split a pasted list the same way the server does (for the live count under the box). */
export function countEntries(text: string): number {
  const seen = new Set<string>();
  for (const raw of text.split(/[\n\r,;\t]+/)) {
    const e = raw.trim();
    if (e) seen.add(e.toLowerCase());
  }
  return seen.size;
}
