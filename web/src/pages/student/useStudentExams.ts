import { pruneExamData, refreshPackageIfChanged, unlockStoredPackages } from "../../lib/offline";
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
      const list = await studentApi.listExams();
      setExams(list);
      setError(null);
      // Audit S-03: packages on this computer get their key while we are online.
      void unlockStoredPackages(list.filter((x) => x.status === "live").map((x) => x.id));
      // Audit W-03: data of handed-in / closed exams leaves this computer.
      pruneExamData(list);
      // Audit W-02: a new start code means a new package.
      for (const x of list) if (x.status === "live") void refreshPackageIfChanged(x.id, x.package_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Cannot reach the server. Retrying…");
      // Audit W-11: keep "not loaded" (null) so pages show the error, not "no exams".
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
