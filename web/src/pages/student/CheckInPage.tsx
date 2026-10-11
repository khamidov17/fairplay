import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { NotLockedBanner } from "../../components/LockBanner";
import { examName, isToday, plural, shortDate, shortTime } from "../../components/student/format";
import { ReadinessChecks, readinessOk, useReadiness } from "../../components/student/Readiness";
import { StartCodeForm } from "../../components/student/StartCodeForm";
import { CheckIcon } from "../../components/student/icons";
import "../../styles/student.css";
import { useStudentExams } from "./useStudentExams";
import { canStartOffline, downloadPackage } from "../../lib/offline";

/** Check-in before an exam: what we can verify about this laptop + the start code. */
export default function CheckInPage() {
  const { examId = "" } = useParams();
  const navigate = useNavigate();
  const { exams, error, reload } = useStudentExams();
  const readiness = useReadiness();
  const exam = exams?.find((e) => e.id === examId) ?? null;
  const ready = readinessOk(readiness);
  // Slice 6: keep an encrypted copy of the exam so a Wi-Fi drop at start does not stop anyone.
  const [offlineReady, setOfflineReady] = useState(false);
  const canPackage = !!exam && exam.status === "live" && !exam.attempt && exam.mode !== "external";
  useEffect(() => {
    if (!canPackage || !exam) return;
    let alive = true;
    // phase 1a: ready only when the key release came too (it needs the exam to be open)
    void downloadPackage(exam.id).then((ok) => alive && setOfflineReady(ok && canStartOffline(exam.id)));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canPackage, exam?.id]);

  let body: JSX.Element;
  if (exams === null) {
    body = (
      <div className="stu-checkin-card" role="status" style={{ maxWidth: 520, width: "100%" }}>
        <p className="stu-muted">Loading the exam…</p>
      </div>
    );
  } else if (!exam) {
    body = (
      <Message title="This exam is not open" text="Wait for your teacher to open it, then check in again.">
        <Link to="/student/exams" className="stu-btn primary">Back to exams</Link>
      </Message>
    );
  } else if (exam.attempt?.status === "submitted") {
    body = (
      <Message title="Already submitted" text={exam.attempt.receipt ? `Your receipt code is ${exam.attempt.receipt}.` : "You already submitted this exam."}>
        <Link to="/student/exams" className="stu-btn primary">Back to exams</Link>
      </Message>
    );
  } else if (exam.attempt?.status === "in_progress") {
    const aid = exam.attempt.attempt_id;
    body = (
      <Message title="Your exam is in progress" text="Your answers are saved. Continue where you stopped — the timer kept running.">
        <button type="button" className="stu-btn primary lg" onClick={() => navigate(`/student/exam/${aid}`)}>
          Continue exam
        </button>
      </Message>
    );
  } else if (exam.status !== "live") {
    body = (
      <Message title="This exam is not open" text="Wait for your teacher to open it, then check in again.">
        <Link to="/student/exams" className="stu-btn primary">Back to exams</Link>
      </Message>
    );
  } else {
    body = (
      <div className="stu-checkin-cards">
        <section className="stu-checkin-card" aria-labelledby="c1-h">
          <div className="stu-step-head">
            <span className={`stu-step-num ${ready ? "ok" : "warn"}`} aria-hidden="true">
              {ready ? <CheckIcon size={16} color="#FFFFFF" /> : "!"}
            </span>
            <h2 id="c1-h">Laptop check</h2>
          </div>
          <ReadinessChecks readiness={readiness} />
          <p className="stu-hint" style={{ marginTop: "auto", lineHeight: 1.5 }}>
            Other apps will close when the exam starts. Save your work in them now.
          </p>
        </section>

        <section className="stu-checkin-card" aria-labelledby="c2-h">
          <div className="stu-step-head">
            <span className="stu-step-num" aria-hidden="true">2</span>
            <h2 id="c2-h">Start code</h2>
          </div>
          <StartCodeForm examId={exam.id} autoFocus onConflict={reload} />
          {offlineReady && (
            <p className="stu-hint" role="status" data-testid="offline-ready">
              Exam saved on this computer. If Wi-Fi drops, you can still start with the code.
            </p>
          )}
        </section>
      </div>
    );
  }

  const meta = exam
    ? [
        exam.scheduled_at && `Starts ${isToday(exam.scheduled_at) ? "today" : shortDate(exam.scheduled_at)} at ${shortTime(exam.scheduled_at)}`,
        `${exam.duration_min} minutes`,
        plural(exam.question_count, "question"),
      ]
        .filter(Boolean)
        .join(" · ")
    : "";

  return (
    <div className="stu stu-darkbg">
      <NotLockedBanner />
      <main className="stu-checkin">
        <Link to="/student/exams" className="stu-checkin-back">
          ← Exams
        </Link>
        <div className="stu-checkin-head">
          <span className="stu-eyebrow">{exam?.room ? `Check-in · Room ${exam.room}` : "Check-in"}</span>
          <h1>{exam ? examName(exam.course, exam.title) : "Exam check-in"}</h1>
          {meta && <span className="meta">{meta}</span>}
        </div>
        {error && (
          <div className="stu-alert warn" role="alert" style={{ maxWidth: 820, width: "100%" }}>
            {error}
          </div>
        )}
        {body}
        <span className="stu-checkin-foot">Problem? Raise your hand and ask your teacher.</span>
      </main>
    </div>
  );
}

function Message({ title, text, children }: { title: string; text: string; children?: ReactNode }) {
  return (
    <section className="stu-checkin-card" style={{ maxWidth: 520, width: "100%", flex: "none" }} aria-live="polite">
      <h2 style={{ margin: 0, fontSize: 20 }}>{title}</h2>
      <p className="stu-muted">{text}</p>
      <div className="stu-row">{children}</div>
    </section>
  );
}
