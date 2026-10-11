// /student/progress — "My progress": topics improving / to work on and recent feedback.
// Only released, teacher-reviewed work counts (never unreviewed AI drafts).
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { studentHomeworkApi, type MyProgress, type TopicProgress } from "../../../api/homework";
import { StudentLayout, StuLoading } from "../../../components/student/StudentLayout";
import { errorText } from "../../../components/teacher/util";
import "../../../styles/homework.css";

export default function MyProgressPage() {
  const [data, setData] = useState<MyProgress | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    studentHomeworkApi.progress().then(setData, (e) => setError(errorText(e)));
  }, []);

  return (
    <StudentLayout>
      <main className="stu-main narrow">
        <div className="stu-head">
          <h1 className="stu-h1">My progress</h1>
          <p className="stu-sub">From your homework and exams your teacher has given back. Recent work counts more.</p>
        </div>
        {error && (
          <div className="stu-alert error" role="alert">
            {error}
          </div>
        )}
        {!data && !error ? (
          <StuLoading label="Loading your progress…" />
        ) : data && data.topics.length === 0 ? (
          <section className="stu-empty">
            <h2>Nothing yet</h2>
            <p className="stu-muted">When your teacher releases feedback, your topics show up here.</p>
          </section>
        ) : data ? (
          <>
            <div className="hw-stu-progress">
              <TopicList title="Getting better" rows={data.improving} empty="Keep going — improvements show up after a few assignments." />
              <TopicList title="To work on" rows={data.work_on} empty="Nothing below 60 % right now. Nice." />
              <TopicList title="Strong" rows={data.strong} empty="No strong topics yet." />
            </div>
            {data.mistakes.length > 0 && (
              <section className="stu-card" aria-labelledby="mm-h">
                <h2 id="mm-h">Mistakes to watch</h2>
                <ul className="hw-stu-mistakes">
                  {data.mistakes.map((m) => (
                    <li key={m.label}>
                      <strong>{m.label}</strong>
                      <span className="stu-muted stu-small">
                        {m.count} time{m.count === 1 ? "" : "s"}
                        {m.topics.length ? ` · ${m.topics.join(", ")}` : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section className="stu-card" aria-labelledby="rf-h">
              <h2 id="rf-h">Recent feedback</h2>
              <div className="stu-rows">
                {data.recent.map((r) => (
                  <Link key={r.exam_id} to={`/student/grades/${r.exam_id}`} className="stu-result-row">
                    <span className="stu-result-title">
                      {r.title}
                      <small>{r.homework ? "Homework" : "Exam"}</small>
                    </span>
                    <span className="stu-result-score">{r.percent}%</span>
                  </Link>
                ))}
              </div>
            </section>
          </>
        ) : null}
      </main>
    </StudentLayout>
  );
}

function TopicList({ title, rows, empty }: { title: string; rows: TopicProgress[]; empty: string }) {
  return (
    <section className="stu-card" aria-label={title}>
      <h2>{title}</h2>
      {rows.length === 0 ? (
        <p className="stu-muted stu-small">{empty}</p>
      ) : (
        <ul className="hw-stu-topics">
          {rows.map((t) => (
            <li key={t.topic}>
              <span>{t.topic}</span>
              <span className="hw-stu-meter" aria-hidden="true">
                <span style={{ width: `${Math.max(4, Math.min(100, t.mastery ?? 0))}%` }} />
              </span>
              <span className="stu-small">{t.mastery != null ? `${Math.round(t.mastery)}%` : "—"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
