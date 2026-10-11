// /teacher/homework: paper assignments with hand-in and review progress.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { classesApi, type ClassSummary } from "../../../api/classes";
import { homeworkApi, type HomeworkSummary } from "../../../api/homework";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, formatWhen } from "../../../components/teacher/util";
import "../../../styles/homework.css";

export function summaryStatus(h: Pick<HomeworkSummary, "grades_released" | "counts" | "past_due">): { label: string; tone: string } {
  if (h.grades_released) return { label: "Feedback released", tone: "blue" };
  const toReview = h.counts.ai_graded + h.counts.submitted;
  if (toReview > 0) return { label: `${toReview} to review`, tone: "orange" };
  if (h.counts.handed_in > 0 && h.counts.reviewed === h.counts.handed_in) return { label: "Ready to release", tone: "green" };
  return h.past_due ? { label: "Past due", tone: "grey" } : { label: "Collecting", tone: "grey" };
}

export default function HomeworkListPage() {
  const [classes, setClasses] = useState<ClassSummary[]>([]);
  const [classId, setClassId] = useState<number>(0);
  const [rows, setRows] = useState<HomeworkSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    classesApi.list().then(setClasses, () => undefined);
  }, []);
  useEffect(() => {
    let alive = true;
    setRows(null);
    homeworkApi.list(classId || undefined).then(
      (r) => alive && setRows(r),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [classId]);

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Homework</h1>
          <p>Paper assignments: students photograph their work, the AI grades every step, you review and release the same day.</p>
        </div>
        <div className="t-head-actions">
          {classes.length > 0 && (
            <select aria-label="Class" className="input" value={classId} onChange={(e) => setClassId(Number(e.target.value))}>
              <option value={0}>All classes</option>
              {classes.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          )}
          <Link to={`/teacher/homework/new${classId ? `?class=${classId}` : ""}`} className="btn btn-primary">
            New homework
          </Link>
        </div>
      </header>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {!rows && !error ? (
        <Loading label="Loading homework…" />
      ) : rows && rows.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>No homework yet</strong>
            Create a paper assignment for a class. Students hand it in from their phone.
          </div>
        </div>
      ) : rows ? (
        <div className="t-card t-table-scroll">
          <table className="t-table">
            <thead>
              <tr>
                <th>Homework</th>
                <th>Classes</th>
                <th>Due</th>
                <th className="num">Handed in</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((h) => {
                const st = summaryStatus(h);
                return (
                  <tr key={h.exam_id}>
                    <td>
                      <Link className="t-row-link strong" to={`/teacher/homework/${h.exam_id}`}>
                        {h.title}
                      </Link>
                    </td>
                    <td>{h.classes.map((c) => c.name).join(", ")}</td>
                    <td>{formatWhen(h.due_at)}</td>
                    <td className="num">
                      {h.counts.handed_in} / {h.counts.total}
                      {h.counts.late > 0 && <span className="t-muted t-small"> · {h.counts.late} late</span>}
                    </td>
                    <td>
                      <Pill tone={st.tone}>{st.label}</Pill>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
