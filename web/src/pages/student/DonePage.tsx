import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { studentApi } from "../../api/client";
import { clockTime, shortDate } from "../../components/student/format";
import { CheckIcon } from "../../components/student/icons";
import { StudentLayout } from "../../components/student/StudentLayout";
import type { DoneState } from "./ExamTakingPage";

export default function DonePage() {
  const { attemptId = "" } = useParams();
  const location = useLocation();
  const fromState = location.state as DoneState | null;
  const [info, setInfo] = useState<DoneState | null>(fromState?.receipt ? fromState : null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    if (info) return;
    // Opened directly (e.g. after reload): find the receipt in the exam list.
    studentApi.listExams().then(
      (list) => {
        const exam = list.find((e) => e.attempt?.attempt_id === attemptId);
        if (exam?.attempt?.receipt) {
          setInfo({ receipt: exam.attempt.receipt, submitted_at: exam.attempt.submitted_at ?? "", title: exam.title });
        } else setMissing(true);
      },
      () => setMissing(true),
    );
  }, [attemptId, info]);

  const at = clockTime(info?.submitted_at);
  const day = shortDate(info?.submitted_at);
  let reached = "Your exam reached the server.";
  if (info && at) {
    if (info.total !== undefined && info.answered !== undefined) {
      reached =
        info.answered === info.total
          ? `All ${info.total} answers reached the server at ${at}.`
          : `Your exam (${info.answered} of ${info.total} questions answered) reached the server at ${at}.`;
    } else reached = `Your exam reached the server ${day ? `on ${day} ` : ""}at ${at}.`;
  }

  return (
    <StudentLayout>
      <main className="stu-main">
        <section className="stu-done" aria-labelledby="done-h">
          <span className="stu-done-check" aria-hidden="true">
            <CheckIcon size={28} color="currentColor" />
          </span>
          <h1 id="done-h">Submitted</h1>
          {info?.title && <p className="stu-done-title">{info.title}</p>}
          <p className="stu-done-text">{reached}</p>
          <div className="stu-receipt">
            <span>Your receipt code</span>
            <span className="code" data-testid="receipt">
              {info?.receipt ?? (missing ? "—" : "…")}
            </span>
          </div>
          {missing && <p className="stu-muted stu-small">The receipt could not be loaded. It is also shown under Exams, Past.</p>}
          <p className="stu-done-foot">Your laptop unlocks now. Grades appear in Grades when your teacher releases them.</p>
          <Link to="/student" className="stu-btn" style={{ marginTop: 12 }}>
            Back to home
          </Link>
        </section>
      </main>
    </StudentLayout>
  );
}
