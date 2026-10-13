import { useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { StudentExamSummary } from "../../api/types";
import { NotLockedBanner } from "../../components/LockBanner";
import { examName, isToday, monthDay, plural, shortDate, shortTime } from "../../components/student/format";
import { PastExamRow } from "../../components/student/ResultRow";
import { StudentLayout, StuLoading } from "../../components/student/StudentLayout";
import { pastExams, upcomingExams, useStudentExams } from "./useStudentExams";

type Tab = "upcoming" | "past";

export default function ExamsPage() {
  const { exams, error } = useStudentExams();
  const [tab, setTab] = useState<Tab>("upcoming");
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ upcoming: null, past: null });

  const upcoming = exams ? upcomingExams(exams) : [];
  const past = exams ? pastExams(exams) : [];
  const tabs: { id: Tab; label: string }[] = [
    { id: "upcoming", label: `Upcoming · ${upcoming.length}` },
    { id: "past", label: `Past · ${past.length}` },
  ];

  const onTabKey = (e: KeyboardEvent) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const next: Tab = tab === "upcoming" ? "past" : "upcoming";
    setTab(next);
    tabRefs.current[next]?.focus();
  };

  return (
    <>
      <NotLockedBanner />
      <StudentLayout>
        <main className="stu-main" style={{ gap: 24 }}>
          <h1 className="stu-h1">Exams</h1>

          <div role="tablist" aria-label="Exam lists" className="stu-tabs" onKeyDown={onTabKey}>
            {tabs.map((t) => (
              <button
                key={t.id}
                ref={(el) => {
                  tabRefs.current[t.id] = el;
                }}
                type="button"
                role="tab"
                id={`tab-${t.id}`}
                aria-selected={tab === t.id}
                aria-controls={`panel-${t.id}`}
                tabIndex={tab === t.id ? 0 : -1}
                className="stu-tab"
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>

          {error && (
            <div className="stu-alert warn" role="alert">
              {error}
            </div>
          )}

          {exams === null ? (
            <StuLoading label="Loading exams…" />
          ) : tab === "upcoming" ? (
            <section id="panel-upcoming" role="tabpanel" aria-labelledby="tab-upcoming" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {upcoming.length === 0 ? (
                <div className="stu-card">
                  <h2>No open exams</h2>
                  <p className="stu-muted">When your teacher opens an exam, it shows up here.</p>
                </div>
              ) : (
                upcoming.map((e) => <UpcomingItem key={e.id} exam={e} />)
              )}
            </section>
          ) : (
            <section id="panel-past" role="tabpanel" aria-labelledby="tab-past" className="stu-card" style={{ gap: 6 }}>
              <h2 style={{ marginBottom: 6 }}>Past results</h2>
              {past.length === 0 ? (
                <p className="stu-muted">Exams you submit show up here.</p>
              ) : (
                <div className="stu-rows">
                  {past.map((e) => (
                    <PastExamRow key={e.id} exam={e} />
                  ))}
                </div>
              )}
            </section>
          )}
        </main>
      </StudentLayout>
    </>
  );
}

function UpcomingItem({ exam }: { exam: StudentExamSummary }) {
  const navigate = useNavigate();
  const inProgress = exam.attempt?.status === "in_progress";
  const md = monthDay(exam.scheduled_at);
  const when = inProgress
    ? "In progress"
    : exam.scheduled_at
      ? `Open now · ${isToday(exam.scheduled_at) ? "today" : shortDate(exam.scheduled_at)} ${shortTime(exam.scheduled_at)}`
      : "Open now";
  const meta = [exam.room && `Room ${exam.room}`, `${exam.duration_min} min`, plural(exam.question_count, "question")]
    .filter(Boolean)
    .join(" · ");
  return (
    <article className="stu-exam-item live">
      <div className="stu-date-tile live" aria-hidden="true">
        {md ? (
          <>
            <span className="m">{md.m}</span>
            <span className="d">{md.d}</span>
          </>
        ) : (
          <span className="m">Live</span>
        )}
      </div>
      <div className="stu-exam-item-body">
        <span className="stu-exam-item-when">{when}</span>
        <h2>{examName(exam.course, exam.title)}</h2>
        <span className="stu-exam-item-meta">{meta}</span>
      </div>
      {inProgress ? (
        <button type="button" className="stu-btn primary" onClick={() => navigate(`/student/exam/${encodeURIComponent(exam.attempt!.attempt_id)}`)}>
          Continue
        </button>
      ) : (
        <Link to={`/student/exams/${exam.id}/checkin`} className="stu-btn primary">
          Check in
        </Link>
      )}
    </article>
  );
}
