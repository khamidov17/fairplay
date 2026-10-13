// Slice 6 G: one student's profile — strengths, weak topics, per-topic trends, exam history.
// Time on task is context only and is never treated as a cheating signal.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, insightsApi } from "../../api/client";
import type { ProfilePractice, StudentProfile } from "../../api/types";
import { Loading } from "../../components/Loading";
import { errorText, num, pct, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";
import { useFeatures } from "../../lib/features";
import { Sparkline, practiceForStudent, shortDate } from "../../components/teacher/insights/viz";

export const TIME_NOTE = "context only — not a cheating signal";

export function minutesText(min: number | null | undefined): string {
  if (min == null) return "—";
  if (min < 1) return "under 1 min";
  const m = Math.round(min);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export default function StudentProfilePage() {
  const { username = "" } = useParams();
  const features = useFeatures();
  const [data, setData] = useState<StudentProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    setNotFound(false);
    insightsApi.profile(username).then(
      (d) => alive && setData(d),
      (e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) setNotFound(true);
        else setError(errorText(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [username]);

  const back = (
    <Link to="/teacher/students" className="t-back">
      <Icon name="arrowLeft" size={16} /> All students
    </Link>
  );

  if (notFound)
    return (
      <>
        {back}
        <div className="t-card">
          <div className="t-empty">
            <strong>Student not found</strong>
            This student has not taken any of your exams.
          </div>
        </div>
      </>
    );
  if (error)
    return (
      <>
        {back}
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      </>
    );
  if (!data) return <Loading label="Loading profile…" />;

  const th = data.thresholds ?? { strength_min: 80, weak_below: 60, min_answers: 2 };
  const topics = data.topics.filter((t) => !t.untagged);
  const practice = data.practice ?? [];

  return (
    <>
      {back}
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>{data.student.name}</h1>
          <p>
            {data.student.username} · {plural(data.exams.length, "exam")} · average {pct(data.average_percent)}
          </p>
        </div>
        {features.has("practice") && (
          <div className="t-head-actions">
            <Link className="btn btn-primary" to={practiceForStudent(data.student.username)}>
              Make practice for weak topics
            </Link>
          </div>
        )}
      </header>

      <div className="ins-stack">
        <div className="ins-two">
          <section className="t-card" aria-labelledby="pf-strong-h">
            <h2 id="pf-strong-h">Strengths</h2>
            <p className="t-small t-muted" style={{ margin: 0 }}>
              {th.strength_min}% or more, over at least {th.min_answers} answers.
            </p>
            {data.strengths.length ? (
              <ul className="ins-chips" aria-label="Strengths">
                {data.strengths.map((t) => (
                  <li key={t} className="ins-chip strong">
                    <Icon name="check" size={14} /> {t}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="t-muted">None yet.</p>
            )}
          </section>
          <section className="t-card" aria-labelledby="pf-weak-h">
            <h2 id="pf-weak-h">Needs work</h2>
            <p className="t-small t-muted" style={{ margin: 0 }}>
              Below {th.weak_below}%, over at least {th.min_answers} answers.
            </p>
            {data.weak.length ? (
              <ul className="ins-chips" aria-label="Weak topics">
                {data.weak.map((t) => (
                  <li key={t} className="ins-chip weak">
                    <Icon name="flag" size={14} /> {t}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="t-muted">No weak topics.</p>
            )}
          </section>
        </div>

        <section className="t-card" aria-labelledby="pf-topics-h">
          <h2 id="pf-topics-h">Topics over time</h2>
          {topics.length === 0 ? (
            <p className="t-muted">No graded answers with topics yet.</p>
          ) : (
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Topic</th>
                    <th scope="col" className="num">
                      Average
                    </th>
                    <th scope="col" className="num">
                      Answers
                    </th>
                    <th scope="col">Trend by exam (line at 60%)</th>
                  </tr>
                </thead>
                <tbody>
                  {topics.map((t) => (
                    <tr key={t.topic}>
                      <td className="strong">{t.topic}</td>
                      <td className="num">{pct(t.avg_percent)}</td>
                      <td className="num">{t.n}</td>
                      <td>
                        <Sparkline
                          label={`${t.topic} trend`}
                          points={t.trend.map((p) => ({ label: `${p.title} (${shortDate(p.date)})`, percent: p.percent }))}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="t-card" aria-labelledby="pf-exams-h">
          <h2 id="pf-exams-h">Exam history</h2>
          {data.exams.length === 0 ? (
            <p className="t-muted">No submitted exams yet.</p>
          ) : (
            <div className="t-table-scroll">
              <table className="t-table">
                <thead>
                  <tr>
                    <th scope="col">Exam</th>
                    <th scope="col">Date</th>
                    <th scope="col" className="num">
                      Score
                    </th>
                    <th scope="col" className="num">
                      Percent
                    </th>
                    <th scope="col">
                      Time on task <span className="ins-note">({TIME_NOTE})</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.exams.map((e) => (
                    <tr key={e.exam_id}>
                      <td>
                        <Link
                          to={`/teacher/exams/${encodeURIComponent(e.exam_id)}/submissions/${encodeURIComponent(e.attempt_id)}`}
                          className="t-row-link"
                        >
                          {e.title}
                        </Link>
                        {e.course && <div className="t-small t-muted">{e.course}</div>}
                      </td>
                      <td>{shortDate(e.date)}</td>
                      <td className="num">
                        {num(e.score)}/{num(e.total_points)}
                      </td>
                      <td className="num">
                        {pct(e.percent)}
                        {e.state === "needs_grading" && <div className="t-small t-muted">so far · needs grading</div>}
                      </td>
                      <td>{minutesText(e.time_on_task_min)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="t-small t-muted" style={{ margin: 0 }}>
            Time on task is how long the exam was open on the computer. It is shown for context only and is not a
            cheating signal. Paper and website exams have no time.
          </p>
        </section>

        {practice.length > 0 && (
          <section className="t-card" aria-labelledby="pf-practice-h">
            <h2 id="pf-practice-h">Practice</h2>
            <p className="t-small t-muted" style={{ margin: 0 }}>
              Practice never counts toward grades.
            </p>
            <ul className="ins-practice">
              {practice.map((p, i) => (
                <PracticeRow key={String(p.id ?? i)} p={p} />
              ))}
            </ul>
          </section>
        )}
      </div>
    </>
  );
}

function PracticeRow({ p }: { p: ProfilePractice }) {
  const result =
    p.percent != null
      ? pct(p.percent)
      : p.correct != null && p.total != null
        ? `${p.correct}/${p.total} correct`
        : p.status ?? "";
  return (
    <li>
      <strong>{p.title || (p.topics?.length ? p.topics.join(", ") : "Practice set")}</strong>
      <span className="t-muted t-small">
        {" "}
        · {[result, p.date ? shortDate(p.date) : null].filter(Boolean).join(" · ")}
      </span>
    </li>
  );
}
