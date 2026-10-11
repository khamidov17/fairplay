import { useEffect, useState } from "react";
import { scanAiApi, type Struggles } from "../../../api/scanAi";
import { errorText, num, plural } from "../../../components/teacher/util";
import "../../../styles/scanAi.css";

/** "Where students struggled": per question, average score and the most common misconceptions. */
export function StrugglePanel({ examId }: { examId: string }) {
  const [data, setData] = useState<Struggles | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    scanAiApi.struggles(examId).then((d) => alive && setData(d), (e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [examId]);

  if (error) return <p className="t-inline-error">{error}</p>;
  if (!data) return null;
  const withData = data.questions.filter((q) => q.graded > 0 || q.misconceptions.length > 0);
  if (withData.length === 0) return null;
  const maxCount = Math.max(1, ...data.questions.flatMap((q) => q.misconceptions.map((m) => m.count)));

  return (
    <section className="t-card" aria-labelledby="st-h">
      <h3 id="st-h">Where students struggled</h3>
      <p className="t-muted sc-small">
        Average score per question and the mistakes behind lost points. Tags from AI suggestions you have not approved
        yet are counted but marked.
      </p>
      <ol className="st-list">
        {[...withData]
          .sort((a, b) => (a.avg_percent ?? 101) - (b.avg_percent ?? 101))
          .map((q) => (
            <li key={q.question_id} className="st-q">
              <div className="st-q-head">
                <strong>Question {q.index + 1}</strong>
                <span className="st-avg">
                  {q.avg_points === null
                    ? "not graded yet"
                    : `${num(q.avg_points)} / ${num(q.points)} avg · ${q.avg_percent}%`}{" "}
                  <span className="t-muted">({q.graded} of {q.total} graded)</span>
                </span>
              </div>
              <p className="st-q-prompt">{q.prompt}</p>
              {q.misconceptions.length > 0 ? (
                <ul className="st-tags">
                  {q.misconceptions.map((m) => (
                    <li key={m.tag}>
                      <span className="st-bar" style={{ width: `${Math.round((m.count / maxCount) * 80) + 8}px` }} aria-hidden="true" />
                      <strong>{m.tag}</strong> · {plural(m.count, "student")}
                      {m.approved < m.count && <span className="t-muted"> ({m.count - m.approved} not approved yet)</span>}
                      <span className="t-muted"> — {m.students.map((s) => s.name).join(", ")}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="t-muted sc-small">No common mistakes recorded.</p>
              )}
            </li>
          ))}
      </ol>
    </section>
  );
}
