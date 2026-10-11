// Slice 6 G: the teacher's students (GET /api/students) with links to each profile.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { insightsApi } from "../../api/client";
import type { StudentSummary } from "../../api/types";
import { Loading } from "../../components/Loading";
import { errorText, pct, plural } from "../../components/teacher/util";
import { profilePath, shortDate } from "../../components/teacher/insights/viz";

export default function StudentsPage() {
  const [rows, setRows] = useState<StudentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    let alive = true;
    insightsApi.students().then(
      (r) => alive && setRows(r),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, []);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!rows) return [];
    if (!q) return rows;
    return rows.filter((s) => s.name.toLowerCase().includes(q) || s.username.toLowerCase().includes(q));
  }, [rows, search]);

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Students</h1>
          <p>{rows ? plural(rows.length, "student") : "Everyone who took your exams or is in your linked courses."}</p>
        </div>
        <div className="t-head-actions">
          <label className="t-gb-filter" htmlFor="stu-search">
            Search
            <input
              id="stu-search"
              className="t-select"
              type="search"
              placeholder="Name or username"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      {!rows && !error ? (
        <Loading label="Loading students…" />
      ) : rows && rows.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>No students yet</strong>
            Students show up here after they take one of your exams or when you link an LMS course.
          </div>
        </div>
      ) : rows ? (
        <section className="t-card" aria-label="Students">
          <div className="t-table-scroll">
            <table className="t-table">
              <thead>
                <tr>
                  <th scope="col">Student</th>
                  <th scope="col" className="num">
                    Exams
                  </th>
                  <th scope="col" className="num">
                    Average
                  </th>
                  <th scope="col">Weakest topic</th>
                  <th scope="col">Last exam</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((s) => (
                  <tr key={s.username}>
                    <td>
                      <Link to={profilePath(s.username)} className="t-row-link">
                        {s.name}
                      </Link>
                      <div className="t-small t-muted">{s.username}</div>
                    </td>
                    <td className="num">{s.exam_count}</td>
                    <td className="num">{pct(s.average_percent)}</td>
                    <td>
                      {s.weakest_topic ? (
                        <>
                          {s.weakest_topic.topic}
                          <span className="t-muted t-small"> · {pct(s.weakest_topic.avg_percent)}</span>
                        </>
                      ) : (
                        <span className="t-muted">—</span>
                      )}
                    </td>
                    <td>{shortDate(s.last_exam_at)}</td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr>
                    <td colSpan={5} className="t-muted">
                      No student matches “{search}”.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </>
  );
}
