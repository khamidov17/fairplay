import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { teacherApi } from "../../api/client";
import type { GradingSummary } from "../../api/types";
import { Loading } from "../../components/Loading";
import { Pill } from "../../components/teacher/TeacherLayout";
import { errorText, plural } from "../../components/teacher/util";

export default function GradingListPage() {
  const [list, setList] = useState<GradingSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    teacherApi.gradingSummary().then(
      (l) => alive && setList(l),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, []);

  const pendingTotal = list?.reduce((s, g) => s + g.pending, 0) ?? 0;

  return (
    <>
      <header className="t-page-head">
        <div className="t-head-text">
          <h1>Grading</h1>
          <p>
            {list === null
              ? "Essays and short answers that need your judgement."
              : pendingTotal > 0
                ? `${plural(pendingTotal, "answer")} waiting across ${plural(list.filter((g) => g.pending > 0).length, "exam")}.`
                : "You are all caught up."}
          </p>
        </div>
      </header>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <section className="t-card" aria-labelledby="gl-h">
        <h2 id="gl-h">Exams with submissions</h2>
        {list === null && !error ? (
          <Loading label="Loading…" />
        ) : !list || list.length === 0 ? (
          <div className="t-empty">
            <strong>No submissions yet</strong>
            Exams appear here once students have submitted.
          </div>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table" style={{ minWidth: 600 }}>
              <thead>
                <tr>
                  <th scope="col">Exam</th>
                  <th scope="col">Course</th>
                  <th scope="col" className="num">
                    Submitted
                  </th>
                  <th scope="col">To grade</th>
                  <th scope="col">Grades</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((g) => (
                  <tr key={g.exam_id}>
                    <td>
                      <Link className="t-row-link" to={`/teacher/exams/${g.exam_id}?tab=results`}>
                        {g.title}
                      </Link>
                      {g.status === "live" && (
                        <span style={{ marginLeft: 8 }}>
                          <Pill tone="green" live>
                            Live
                          </Pill>
                        </span>
                      )}
                    </td>
                    <td>{g.course || <span className="t-muted">—</span>}</td>
                    <td className="num">{g.submitted}</td>
                    <td>{g.pending > 0 ? <Pill tone="orange">{g.pending} to grade</Pill> : <Pill tone="green">Done</Pill>}</td>
                    <td>{g.grades_released ? <Pill tone="blue">Released</Pill> : <span className="t-muted">Not released</span>}</td>
                    <td style={{ textAlign: "right" }}>
                      <Link
                        to={`/teacher/exams/${g.exam_id}/grading`}
                        className={g.pending > 0 ? "btn btn-primary" : "t-btn-outline"}
                        aria-label={`${g.pending > 0 ? "Grade" : "Review"} ${g.title}`}
                      >
                        {g.pending > 0 ? "Grade" : "Review"}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
