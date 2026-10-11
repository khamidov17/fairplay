import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { teacherApi } from "../../api/client";
import type { Exam, GradingSummary } from "../../api/types";
import { Loading } from "../../components/Loading";
import { ExamsTable } from "../../components/teacher/ExamsTable";
import { errorText, formatWhen, hms, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";
import { useMe } from "../../lib/useMe";

function greeting(d = new Date()): string {
  const h = d.getHours();
  if (h < 5) return "Good evening";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

/** The live exam, else the next scheduled draft in the future. */
export function pickFeatured(exams: Exam[], now = Date.now()): Exam | null {
  const live = exams.find((e) => e.status === "live");
  if (live) return live;
  const upcoming = exams
    .filter((e) => e.status === "draft" && e.scheduled_at && new Date(e.scheduled_at).getTime() > now)
    .sort((a, b) => new Date(a.scheduled_at!).getTime() - new Date(b.scheduled_at!).getTime());
  return upcoming[0] ?? null;
}

const HOME_LIMIT = 8;

export default function TeacherHome() {
  const me = useMe("teacher");
  const [exams, setExams] = useState<Exam[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [grading, setGrading] = useState<GradingSummary[] | null>(null);
  const [gradingError, setGradingError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    let alive = true;
    teacherApi.listExams().then(
      (list) => alive && setExams(list),
      (e) => {
        if (!alive) return;
        setError(errorText(e));
        setExams([]);
      },
    );
    teacherApi.gradingSummary().then(
      (list) => alive && setGrading(list),
      (e) => alive && setGradingError(errorText(e, "Could not load grading.")),
    );
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive = false;
      clearInterval(tick);
    };
  }, []);

  const featured = useMemo(() => (exams ? pickFeatured(exams, now) : null), [exams, now]);
  const liveCount = exams?.filter((e) => e.status === "live").length ?? 0;
  const toGrade = grading?.reduce((s, g) => s + (g.pending || 0), 0) ?? 0;

  const summary: string[] = [];
  if (liveCount) summary.push(`${plural(liveCount, "exam")} live right now`);
  if (toGrade) summary.push(`${plural(toGrade, "answer")} to grade`);
  const subline = summary.length ? `You have ${summary.join(" and ")}.` : "Create an exam, open it in class, and read the start code aloud.";

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text" style={{ gap: 4 }}>
          <h1>
            {greeting()}
            {me ? `, ${me.name}` : ""}
          </h1>
          <p>{subline}</p>
        </div>
        <Link to="/teacher/exams/new" className="btn btn-primary">
          <Icon name="plus" size={18} />
          Create
        </Link>
      </header>

      <section aria-label="Start something" className="t-actions-grid">
        <Link to="/teacher/exams/new" className="t-action-card">
          <span className="t-action-icon orange">
            <Icon name="exams" />
          </span>
          <span className="t-action-title">Create a new exam</span>
          <span className="t-action-sub">Write questions here, then open it in class with a start code.</span>
        </Link>
        <Link to="/teacher/grading" className="t-action-card">
          <span className="t-action-icon blue">
            <Icon name="grading" />
          </span>
          <span className="t-action-title">Grade answers</span>
          <span className="t-action-sub">Essays and short answers that need your judgement.</span>
        </Link>
        <Link to="/teacher/gradebook" className="t-action-card">
          <span className="t-action-icon green">
            <Icon name="gradebook" />
          </span>
          <span className="t-action-title">Open the gradebook</span>
          <span className="t-action-sub">Every student and exam in one table. Export to CSV.</span>
        </Link>
      </section>

      {featured && <FeaturedBanner exam={featured} now={now} />}

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <div className="t-columns">
        <section aria-labelledby="exams-h" className="t-card t-col-3">
          <div className="t-card-head">
            <h2 id="exams-h">Your exams</h2>
            {exams && exams.length > HOME_LIMIT && (
              <Link to="/teacher/exams" className="t-card-link">
                See all {exams.length}
              </Link>
            )}
          </div>
          {exams === null ? (
            <Loading label="Loading exams…" />
          ) : exams.length === 0 ? (
            <div className="t-empty">
              <strong>No exams yet</strong>
              Your first exam takes about two minutes to set up.
              <div style={{ marginTop: 14 }}>
                <Link to="/teacher/exams/new" className="btn btn-primary">
                  Create exam
                </Link>
              </div>
            </div>
          ) : (
            <ExamsTable exams={exams.slice(0, HOME_LIMIT)} caption="Your exams" />
          )}
        </section>

        <section aria-labelledby="grade-h" className="t-card t-col-2" style={{ gap: 18 }}>
          <div className="t-card-head">
            <h2 id="grade-h">Needs grading</h2>
            <Link to="/teacher/grading" className="t-card-link">
              Open
            </Link>
          </div>
          <NeedsGrading grading={grading} error={gradingError} />
        </section>
      </div>
    </>
  );
}

function FeaturedBanner({ exam, now }: { exam: Exam; now: number }) {
  const isLive = exam.status === "live";
  const startsIn = exam.scheduled_at ? new Date(exam.scheduled_at).getTime() - now : 0;
  const title = exam.course ? `${exam.course} · ${exam.title}` : exam.title;
  const details = [exam.room && `Room ${exam.room}`, plural(exam.question_count, "question"), `${exam.duration_min} min`]
    .filter(Boolean)
    .join(" · ");
  return (
    <section aria-labelledby="featured-h" className="t-banner">
      <div className="t-banner-text">
        <span className="t-banner-eyebrow">
          {isLive ? "Live now" : `Next exam · ${formatWhen(exam.scheduled_at)}`}
        </span>
        <h2 id="featured-h">{title}</h2>
        <span className="t-banner-sub">{details} · Full-screen lockdown</span>
      </div>
      {isLive ? (
        <div className="t-dark-box">
          <span className="t-dark-box-label">Start code</span>
          <span className="t-dark-box-value" style={{ letterSpacing: "0.2em" }}>
            {exam.start_code ?? "----"}
          </span>
        </div>
      ) : (
        <div className="t-dark-box">
          <span className="t-dark-box-label">Starts in</span>
          <span className="t-dark-box-value" aria-live="off">
            {hms(startsIn)}
          </span>
        </div>
      )}
      <Link to={`/teacher/exams/${exam.id}`} className="t-btn-light">
        {isLive ? "Open live room" : "Prepare exam"}
      </Link>
    </section>
  );
}

function NeedsGrading({ grading, error }: { grading: GradingSummary[] | null; error: string | null }) {
  if (error) return <p className="t-inline-error">{error}</p>;
  if (grading === null) return <p className="t-muted t-small">Loading…</p>;
  const pending = grading.filter((g) => g.pending > 0);
  if (pending.length === 0)
    return (
      <div className="t-empty">
        <strong>All caught up</strong>
        Essays and unmatched short answers show up here after students submit.
      </div>
    );
  return (
    <>
      {pending.slice(0, 6).map((g) => (
        <div key={g.exam_id} className="t-kv" style={{ alignItems: "center" }}>
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <Link to={`/teacher/exams/${g.exam_id}/grading`} className="t-card-link" style={{ color: "var(--text)" }}>
              {g.course ? `${g.course} · ` : ""}
              {g.title}
            </Link>
            <span className="t-small t-muted" style={{ fontWeight: 400 }}>
              {plural(g.submitted, "student")} submitted{g.status === "live" ? " · still live" : ""}
            </span>
          </span>
          <span className="t-pill orange">{g.pending} to grade</span>
        </div>
      ))}
    </>
  );
}
