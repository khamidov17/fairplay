// /teacher/progress/students/:username — one student's topic mastery over time, recurring
// mistakes with pictures of their own answers, and the teacher's notes.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { progressApi, type StudentProgress } from "../../../api/homework";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, formatWhen, plural } from "../../../components/teacher/util";
import "../../../styles/homework.css";
import { cellClass, practiceLink, sparkPoints, trendText } from "./heatmap";

export default function StudentProgressPage() {
  const { username = "" } = useParams();
  const [data, setData] = useState<StudentProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const load = useCallback(() => {
    progressApi.student(username).then(setData, (e) => setError(errorText(e)));
  }, [username]);
  useEffect(load, [load]);

  async function addNote(e: FormEvent) {
    e.preventDefault();
    if (!note.trim()) return;
    try {
      await progressApi.addNote(username, note.trim());
      setNote("");
      load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  if (!data) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading progress…" />;
  const weak = data.topics.filter((t) => t.level === "struggling").map((t) => t.topic);

  return (
    <>
      <Link to={data.classes[0] ? `/teacher/progress?class=${data.classes[0].id}` : "/teacher/progress"} className="t-back">
        ← Progress
      </Link>
      <header className="t-page-head">
        <div className="t-head-text">
          <span className="t-eyebrow">{data.classes.map((c) => c.name).join(", ") || "Student"}</span>
          <h1>{data.student.name}</h1>
          <p>
            {data.mastery != null ? `${data.mastery}% overall` : "No graded work yet"} · {plural(data.assignments.length, "assignment")} ·
            trend {trendText(data.trend, data.trend_label)}
          </p>
        </div>
        <div className="t-head-actions">
          {weak.length > 0 && (
            <Link className="btn btn-secondary" to={practiceLink([data.student.username], weak.join(","))}>
              Practice weak topics
            </Link>
          )}
          <Link className="btn btn-secondary" to={`/teacher/students/${encodeURIComponent(data.student.username)}`}>
            Full profile
          </Link>
        </div>
      </header>

      <section className="t-card" aria-labelledby="sp-topics-h">
        <h2 id="sp-topics-h">Topics over time</h2>
        {data.topics.length === 0 ? (
          <p className="t-muted">Nothing graded yet.</p>
        ) : (
          <ul className="sp-topics">
            {data.topics.map((t) => (
              <li key={t.topic}>
                <span className="sp-topic-name">{t.topic}</span>
                <svg className="sp-spark" viewBox="0 0 120 32" width="120" height="32" role="img"
                  aria-label={`${t.topic}: ${t.series.map((s) => `${s.percent}%`).join(", ")}`}>
                  <line x1="0" x2="120" y1={3 + (40 * 26) / 100} y2={3 + (40 * 26) / 100} className="sp-ref" />
                  <polyline points={sparkPoints(t.series)} className="sp-line" />
                  {t.series.length === 1 && <circle cx="60" cy={sparkPoints(t.series).split(",")[1]} r="2.5" className="sp-dot" />}
                </svg>
                <span className={cellClass(t.mastery)}>{t.mastery != null ? `${Math.round(t.mastery)}%` : "—"}</span>
                <span className={`hm-trend ${t.trend_label}`}>{trendText(t.trend, t.trend_label)}</span>
                <span className="t-muted t-small">{plural(t.n, "answer")}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="t-card" aria-labelledby="sp-mis-h">
        <h2 id="sp-mis-h">Recurring mistakes</h2>
        {data.mistakes.length === 0 ? (
          <p className="t-muted">No tagged mistakes.</p>
        ) : (
          <ul className="sp-mistakes">
            {data.mistakes.map((m) => (
              <li key={m.label}>
                <div className="sp-mis-head">
                  <strong>{m.label}</strong>
                  <span className="t-muted t-small">
                    {plural(m.count, "time")} · {m.topics.join(", ")} · last {formatWhen(m.last_seen)}
                  </span>
                  {m.recurring && <Pill tone="orange">Recurring</Pill>}
                </div>
                <div className="sp-examples">
                  {m.examples.map((ex) => (
                    <figure key={`${ex.exam_id}-${ex.question_id}`}>
                      {ex.image ? (
                        <a href={ex.image} target="_blank" rel="noreferrer">
                          <img src={ex.image} alt={`Answer to: ${ex.prompt}`} loading="lazy" />
                        </a>
                      ) : (
                        <div className="sp-noimg">Typed answer</div>
                      )}
                      <figcaption>
                        <span>{ex.title}</span>
                        <span className="t-muted t-small">{ex.prompt}</span>
                        {!ex.approved && <span className="t-small t-muted">AI draft — not reviewed</span>}
                      </figcaption>
                    </figure>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="t-card" aria-labelledby="sp-notes-h">
        <h2 id="sp-notes-h">Notes</h2>
        <form onSubmit={addNote} className="sp-note-form">
          <label htmlFor="sp-note" className="hw-sr">
            New note
          </label>
          <textarea id="sp-note" className="textarea" rows={2} maxLength={2000} placeholder="e.g. Talked about sign errors after class." value={note}
            onChange={(e) => setNote(e.target.value)} />
          <button type="submit" className="btn btn-secondary" disabled={!note.trim()}>
            Add note
          </button>
        </form>
        <ul className="sp-notes">
          {data.notes.map((n) => (
            <li key={n.id}>
              <span>{n.text}</span>
              <span className="t-muted t-small">{formatWhen(n.created_at)}</span>
              <button type="button" className="t-link-btn" aria-label="Delete note" onClick={async () => {
                await progressApi.deleteNote(n.id);
                load();
              }}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
