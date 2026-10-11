import { useCallback, useEffect, useState } from "react";
import { ApiError, studentApi } from "../../api/client";
import type { StudentExamSummary } from "../../api/types";

export const STUDENT_LIST_POLL_MS = 10000;

/** Student exam list, polled so a newly opened exam shows up without a reload. */
export function useStudentExams(pollMs = STUDENT_LIST_POLL_MS) {
  const [exams, setExams] = useState<StudentExamSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setExams(await studentApi.listExams());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Cannot reach the server. Retrying…");
      setExams((x) => x ?? []);
    }
  }, []);

  useEffect(() => {
    void reload();
    if (!pollMs) return;
    const t = setInterval(() => void reload(), pollMs);
    return () => clearInterval(t);
  }, [reload, pollMs]);

  return { exams, error, reload };
}

/** Live exams the student can still take (or continue), in-progress first. */
export function upcomingExams(exams: StudentExamSummary[]): StudentExamSummary[] {
  const list = exams.filter(
    (e) => e.attempt?.status === "in_progress" || (e.status === "live" && e.attempt?.status !== "submitted"),
  );
  return list.sort((a, b) => Number(b.attempt?.status === "in_progress") - Number(a.attempt?.status === "in_progress"));
}

/** Exams this student submitted, newest first. */
export function pastExams(exams: StudentExamSummary[]): StudentExamSummary[] {
  return exams
    .filter((e) => e.attempt?.status === "submitted")
    .sort((a, b) => (b.attempt?.submitted_at ?? "").localeCompare(a.attempt?.submitted_at ?? ""));
}
