import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { StudentExamSummary } from "../../api/types";
import { NotLockedBanner } from "../../components/LockBanner";
import { firstName, examName, plural, shortTime, isToday, shortDate } from "../../components/student/format";
import { ReadinessChecks, useReadiness } from "../../components/student/Readiness";
import { PastExamRow } from "../../components/student/ResultRow";
import { StartCodeForm } from "../../components/student/StartCodeForm";
import { StudentLayout, StuLoading } from "../../components/student/StudentLayout";
import { useMe } from "../../lib/useMe";
import { pastExams, upcomingExams, useStudentExams } from "./useStudentExams";
import { HomeworkHomeCard } from "./homework/HomeworkHomeCard";

export default function StudentHome() {
  const me = useMe("student");
  const { exams, error, reload } = useStudentExams();
  const readiness = useReadiness();
  const [activeId, setActiveId] = useState<string | null>(null);

  const upcoming = exams ? upcomingExams(exams) : [];
  const past = exams ? pastExams(exams) : [];
  const name = firstName(me?.name);

  const summary =
    exams === null
      ? ""
      : upcoming.length === 0
        ? "No exam is open right now."
        : upcoming.length === 1
          ? upcoming[0].attempt?.status === "in_progress"
            ? "You have an exam in progress."
            : "One exam is open now."
          : `${upcoming.length} exams are open now.`;

  return (
    <>
      <NotLockedBanner />
      <StudentLayout>
        <main className="stu-main">
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <h1 className="stu-h1">{name ? `Hi ${name}` : "Hi"}</h1>
            <p className="stu-sub" aria-live="polite">{summary}</p>
          </div>

          <HomeworkHomeCard />

          {error && (
            <div className="stu-alert warn" role="alert">
              {error}
            </div>
          )}

          {exams === null ? (
            <StuLoading label="Loading your exams…" />
          ) : (
            <div className="stu-home-grid">
              {upcoming.length === 0 ? (
                <section className="stu-empty" aria-labelledby="none-h">
                  <h2 id="none-h">No open exams</h2>
                  <p className="stu-muted">When your teacher opens an exam, it shows up here. This page checks every few seconds.</p>
                </section>
              ) : (
                <div className="stu-hero-col">
                  {upcoming.map((exam) => (
                    <ExamHero
                      key={exam.id}
                      exam={exam}
                      active={activeId === exam.id}
                      onActivate={() => setActiveId(exam.id)}
                      onCancel={() => setActiveId(null)}
                      onChanged={reload}
                    />
                  ))}
                </div>
              )}

              <section className="stu-card stu-ready" aria-labelledby="ready-h">
                <h2 id="ready-h">Are you ready?</h2>
                <ReadinessChecks readiness={readiness} />
                <p className="stu-hint">Other apps will close when the exam starts. Save your work in them now.</p>
              </section>
            </div>
          )}

          {past.length > 0 && (
            <section className="stu-card" aria-labelledby="recent-h" style={{ gap: 6 }}>
              <div className="stu-row" style={{ marginBottom: 6 }}>
                <h2 id="recent-h">Recent results</h2>
                <span className="stu-spacer" />
                <Link to="/student/grades" className="stu-link">
                  All grades →
                </Link>
              </div>
              <div className="stu-rows">
                {past.slice(0, 4).map((e) => (
                  <PastExamRow key={e.id} exam={e} />
                ))}
              </div>
            </section>
          )}
        </main>
      </StudentLayout>
    </>
  );
}

function ExamHero({
  exam,
  active,
  onActivate,
  onCancel,
  onChanged,
}: {
  exam: StudentExamSummary;
  active: boolean;
  onActivate: () => void;
  onCancel: () => void;
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const inProgress = exam.attempt?.status === "in_progress";
  const headId = `hero-${exam.id}`;
  const when = inProgress
    ? "In progress"
    : exam.scheduled_at
      ? `Open now · ${isToday(exam.scheduled_at) ? "today" : shortDate(exam.scheduled_at)} ${shortTime(exam.scheduled_at)}`
      : "Open now";
  const meta = [
    exam.class_names?.length ? exam.class_names.join(", ") : "", // classes
    exam.room && `Room ${exam.room}`,
    `${exam.duration_min} minutes`,
    plural(exam.question_count, "question"),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section className="stu-exam-hero" aria-labelledby={headId}>
      <span className="stu-eyebrow">{when}</span>
      <h2 id={headId}>{examName(exam.course, exam.title)}</h2>
      <span className="stu-exam-hero-meta">{meta}</span>

      {inProgress ? (
        <>
          <div className="stu-exam-hero-note">Your answers are saved. Continue where you stopped — the timer kept running.</div>
          <div className="stu-row">
            <button
              type="button"
              className="stu-btn primary lg"
              onClick={() => navigate(`/student/exam/${exam.attempt!.attempt_id}`)}
            >
              Continue exam
            </button>
          </div>
        </>
      ) : active ? (
        <StartCodeForm examId={exam.id} autoFocus onCancel={onCancel} onConflict={onChanged} />
      ) : (
        <>
          <div className="stu-exam-hero-note">Your teacher will read a start code in the room. Type it in and the exam opens.</div>
          <div className="stu-row">
            <button type="button" className="stu-btn primary lg" onClick={onActivate}>
              Enter start code
            </button>
            <Link to={`/student/exams/${exam.id}/checkin`} className="stu-btn on-dark lg">
              Check in
            </Link>
          </div>
        </>
      )}
    </section>
  );
}
