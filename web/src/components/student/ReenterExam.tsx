// Audit 2026-10-12 W-01: after the teacher moved the exam to another computer (or this
// computer lost its exam session), "Continue" gets 409. The only way back in is the start
// code: POST start binds the attempt to this computer with a fresh session.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { studentApi } from "../../api/client";
import { cachedAttemptView } from "../../lib/offline";
import { StartCodeForm } from "./StartCodeForm";

export function ReenterExam({ attemptId, detail }: { attemptId: string; detail: string }) {
  const [examId, setExamId] = useState<string | null>(() => cachedAttemptView(attemptId)?.exam_id ?? null);
  const [lookupFailed, setLookupFailed] = useState(false);

  useEffect(() => {
    if (examId) return;
    let alive = true;
    studentApi.listExams().then(
      (list) => {
        if (!alive) return;
        const hit = list.find((e) => e.attempt?.attempt_id === attemptId);
        if (hit) setExamId(hit.id);
        else setLookupFailed(true);
      },
      () => alive && setLookupFailed(true),
    );
    return () => {
      alive = false;
    };
  }, [attemptId, examId]);

  return (
    <div className="stu stu-exam">
      <main className="stu-center">
        <div className="stu-card" style={{ gap: 14, maxWidth: 560 }}>
          <h1 style={{ margin: 0, fontSize: 22 }}>Enter the start code to continue</h1>
          <p className="stu-muted" style={{ margin: 0 }}>
            {detail} Your saved answers are kept. Type the start code from your teacher to continue on this computer.
          </p>
          {examId ? (
            <StartCodeForm examId={examId} autoFocus legend="Start code" />
          ) : lookupFailed ? (
            <p className="stu-field-error" style={{ margin: 0 }}>
              This exam could not be found from here. Go back to the home page and open it again.
            </p>
          ) : (
            <p className="stu-muted">Finding your exam…</p>
          )}
          <Link to="/student" className="stu-link">
            Back to home
          </Link>
        </div>
      </main>
    </div>
  );
}

/** 409 that means "this computer's exam session is not the current one". */
export function isSessionConflict(status: number, detail: string): boolean {
  return status === 409 && /somewhere else|another computer|session/i.test(detail);
}
