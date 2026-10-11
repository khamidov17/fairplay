import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { teacherApi } from "../../api/client";
import { classesApi, type ClassSummary } from "../../api/classes";
import type { Gradebook, GradebookState } from "../../api/types";
import { Loading } from "../../components/Loading";
import { errorText, num, pct, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";

const STATE_LABEL: Record<GradebookState, string> = {
  graded: "Graded",
  needs_grading: "Needs grading",
  in_progress: "In progress",
  missing: "Missing",
  not_taken: "Not taken",
  submitted: "Submitted",
};

export function cellText(cell: { state: GradebookState; score: number | null; percent: number | null } | undefined): string {
  if (!cell) return "—";
  if (cell.state === "graded") return pct(cell.percent);
  return STATE_LABEL[cell.state];
}

export const CSV_FILENAME = "proctora-gradebook.csv";

export default function GradebookPage() {
  const [course, setCourse] = useState("");
  const [data, setData] = useState<Gradebook | null>(null);
  const [courses, setCourses] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  // classes: optional class filter (only shown when the teacher has classes)
  const [classId, setClassId] = useState("");
  const [classes, setClasses] = useState<ClassSummary[]>([]);
  useEffect(() => {
    let alive = true;
    classesApi.list().then(
      (c) => alive && Array.isArray(c) && setClasses(c),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    (classId ? classesApi.gradebook(course, Number(classId)) : teacherApi.gradebook(course)).then(
      (g) => {
        if (!alive) return;
        setData(g);
        if (g.courses?.length) setCourses(g.courses);
        setLoading(false);
      },
      (e) => {
        if (!alive) return;
        setError(errorText(e));
        setLoading(false);
      },
    );
    return () => {
      alive = false;
    };
  }, [course, classId]);

  async function exportCsv() {
    setExporting(true);
    setExportError(null);
    try {
      const blob = classId
        ? await classesApi.gradebookCsv(course, Number(classId))
        : await teacherApi.gradebookCsv(course);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = CSV_FILENAME;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setExportError(errorText(e, "Could not download the CSV. Try again."));
    } finally {
      setExporting(false);
    }
  }

  const exams = data?.exams ?? [];
  const students = data?.students ?? [];

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Gradebook{course ? ` · ${course}` : ""}</h1>
          <p>
            {data ? `${plural(students.length, "student")} · ${plural(exams.length, "exam")}` : "Every student and exam in one table."}
          </p>
        </div>
        <div className="t-head-actions">
          <label className="t-gb-filter" htmlFor="gb-course">
            Course
            <select id="gb-course" className="t-select" value={course} onChange={(e) => setCourse(e.target.value)}>
              <option value="">All courses</option>
              {courses.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          {classes.length > 0 && (
            <label className="t-gb-filter" htmlFor="gb-class">
              Class
              <select id="gb-class" className="t-select" value={classId} onChange={(e) => setClassId(e.target.value)}>
                <option value="">All students</option>
                {classes.map((c) => (
                  <option key={c.id} value={String(c.id)}>
                    {c.name}
                    {c.section ? ` · ${c.section}` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="button" className="t-btn-outline" onClick={exportCsv} disabled={exporting || !data}>
            <Icon name="download" size={18} />
            {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </div>
      </header>

      {exportError && (
        <div className="alert alert-error" role="alert">
          {exportError}
        </div>
      )}
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <div className="t-legend" aria-label="Legend">
        <span>
          <i className="t-swatch needs_grading" />
          Needs grading
        </span>
        <span>
          <i className="t-swatch in_progress" />
          In progress
        </span>
        <span>
          <i className="t-swatch missing" />
          Missing
        </span>
        <span>
          <i className="t-swatch not_taken" />
          Not taken
        </span>
        <span className="t-muted">· Average counts graded exams only</span>
      </div>

      {loading && !data ? (
        <Loading label="Loading gradebook…" />
      ) : !data ? null : students.length === 0 || exams.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>{exams.length === 0 ? "No exams yet" : "No students yet"}</strong>
            {exams.length === 0 ? "Exams you create show up here as columns." : "Students appear once they have accounts."}
          </div>
        </div>
      ) : (
        <section aria-label="Grades" className="t-gb-wrap" aria-busy={loading}>
          <table className="t-table t-gb" style={{ minWidth: Math.max(560, 220 + exams.length * 140) }}>
            <thead>
              <tr>
                <th scope="col">Student</th>
                {exams.map((e) => (
                  <th scope="col" key={e.exam_id}>
                    <Link to={`/teacher/exams/${e.exam_id}?tab=results`} className="t-row-link" style={{ fontWeight: 600 }}>
                      {e.title}
                    </Link>
                    <span className="t-gb-course">
                      {[e.course, `${e.total_points} pts`, e.grades_released ? "released" : null].filter(Boolean).join(" · ")}
                    </span>
                  </th>
                ))}
                <th scope="col">Average</th>
              </tr>
            </thead>
            <tbody>
              {students.map((s) => (
                <tr key={s.username}>
                  <th scope="row" style={{ color: "var(--text)", fontWeight: 600, borderBottom: "1px solid var(--divider)" }}>
                    {s.name}
                    <span className="t-gb-course">{s.username}</span>
                  </th>
                  {exams.map((e) => {
                    const cell = s.cells[e.exam_id];
                    const state = cell?.state ?? "not_taken";
                    const title =
                      cell && cell.score != null
                        ? `${num(cell.score)} / ${e.total_points} points${state === "needs_grading" ? " so far" : ""}`
                        : STATE_LABEL[state];
                    return (
                      <td key={e.exam_id} className="cell">
                        <span className={`t-cell ${state}`} title={title}>
                          {cellText(cell)}
                        </span>
                      </td>
                    );
                  })}
                  <td style={{ fontWeight: 700 }}>{pct(s.average_percent)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
