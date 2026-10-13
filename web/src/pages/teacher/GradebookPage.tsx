// 2026-10-13: one simple "Grades" page. Top: each exam with answers — grade it, release the
// grades. Below: everyone's marks (all classes or one), with CSV export.
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { classesApi, teacherApi } from "../../api/client";
import type { Gradebook, GradingSummary, SchoolClass } from "../../api/types";
import { Loading } from "../../components/Loading";
import { MarksLegend, MarksTable, downloadCsv } from "../../components/teacher/MarksTable";
import { Pill } from "../../components/teacher/TeacherLayout";
import { errorText, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";

export { cellText } from "../../components/teacher/MarksTable";
export const CSV_FILENAME = "fairplay-grades.csv";

export default function GradebookPage() {
  const [classes, setClasses] = useState<SchoolClass[]>([]);
  const [classId, setClassId] = useState<number | null>(null);
  const [toGrade, setToGrade] = useState<GradingSummary[] | null>(null);
  const [data, setData] = useState<Gradebook | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const loadExams = useCallback(async () => {
    try {
      setToGrade(await teacherApi.gradingSummary());
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void loadExams();
    classesApi.list().then(setClasses, () => setClasses([]));
  }, [loadExams]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    teacherApi.gradebook("", classId).then(
      (g) => {
        if (!alive) return;
        setData(g);
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
  }, [classId]);

  async function release(g: GradingSummary) {
    setBusy(g.exam_id);
    setError(null);
    try {
      await teacherApi.release(g.exam_id);
      await loadExams();
      setData(await teacherApi.gradebook("", classId));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function exportCsv() {
    setExporting(true);
    try {
      await downloadCsv(await teacherApi.gradebookCsv("", classId), CSV_FILENAME);
    } catch (e) {
      setError(errorText(e, "Could not download the CSV. Try again."));
    } finally {
      setExporting(false);
    }
  }

  const className = (id: number | null | undefined) => classes.find((k) => k.id === id)?.name ?? "";
  const shown = (toGrade ?? []).filter((g) => classId == null || g.class_id === classId);
  const pendingTotal = shown.reduce((s, g) => s + g.pending, 0);
  const exams = data?.exams ?? [];
  const students = data?.students ?? [];

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Grades{classId != null && className(classId) ? ` · ${className(classId)}` : ""}</h1>
          <p>
            {toGrade === null
              ? "Check answers, grade them and release grades to students."
              : pendingTotal > 0
                ? `${plural(pendingTotal, "answer")} waiting for you.`
                : "Nothing waiting. You are all caught up."}
          </p>
        </div>
        <div className="t-head-actions">
          <label className="t-gb-filter" htmlFor="gb-class">
            Class
            <select
              id="gb-class"
              className="t-select"
              value={classId ?? ""}
              onChange={(e) => setClassId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">All classes</option>
              {classes.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <section className="t-card" aria-labelledby="gr-exams-h">
        <h2 id="gr-exams-h">1. Grade and release</h2>
        {toGrade === null && !error ? (
          <Loading label="Loading…" />
        ) : shown.length === 0 ? (
          <div className="t-empty">
            <strong>No answers yet</strong>
            Exams show up here once students hand in.
          </div>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table" style={{ minWidth: 620 }}>
              <thead>
                <tr>
                  <th scope="col">Exam</th>
                  <th scope="col">Class</th>
                  <th scope="col" className="num">
                    Handed in
                  </th>
                  <th scope="col">To grade</th>
                  <th scope="col">Students see grades</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {shown.map((g) => {
                  const canRelease = !g.grades_released && g.pending === 0 && g.status === "closed";
                  return (
                    <tr key={g.exam_id}>
                      <td>
                        <Link className="t-row-link" to={`/teacher/exams/${encodeURIComponent(g.exam_id)}?tab=results`}>
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
                      <td>{className(g.class_id) || <span className="t-muted">—</span>}</td>
                      <td className="num">{g.submitted}</td>
                      <td>{g.pending > 0 ? <Pill tone="orange">{g.pending} to grade</Pill> : <Pill tone="green">Done</Pill>}</td>
                      <td>
                        {g.grades_released ? (
                          <Pill tone="blue">Released</Pill>
                        ) : (
                          <span className="t-muted">{g.status === "live" ? "After you close it" : "Not yet"}</span>
                        )}
                      </td>
                      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                        <span style={{ display: "inline-flex", gap: 8 }}>
                          <Link
                            to={`/teacher/exams/${encodeURIComponent(g.exam_id)}/grading`}
                            className={g.pending > 0 ? "btn btn-primary" : "t-btn-outline"}
                            aria-label={`${g.pending > 0 ? "Grade" : "Review"} ${g.title}`}
                          >
                            {g.pending > 0 ? "Grade" : "Review"}
                          </Link>
                          {canRelease && (
                            <button
                              type="button"
                              className="btn btn-primary"
                              disabled={busy !== null}
                              onClick={() => void release(g)}
                              aria-label={`Release grades for ${g.title}`}
                            >
                              {busy === g.exam_id ? "Releasing…" : "Release grades"}
                            </button>
                          )}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="t-card" aria-labelledby="gr-marks-h" style={{ gap: 12 }}>
        <div className="t-card-head">
          <h2 id="gr-marks-h">2. Marks</h2>
          <button type="button" className="t-btn-outline" onClick={exportCsv} disabled={exporting || !data}>
            <Icon name="download" size={18} />
            {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </div>
        <p className="t-muted" style={{ margin: 0 }}>
          {data ? `${plural(students.length, "student")} · ${plural(exams.length, "exam")}` : "Every student and exam in one table."}
        </p>
        <MarksLegend />
        {loading && !data ? (
          <Loading label="Loading marks…" />
        ) : !data ? null : students.length === 0 || exams.length === 0 ? (
          <div className="t-empty">
            <strong>{exams.length === 0 ? "No opened exams yet" : "No students yet"}</strong>
            {exams.length === 0 ? "Exams show up here as columns once you open them." : "Add students to a class first."}
          </div>
        ) : (
          <MarksTable data={data} busy={loading} />
        )}
      </section>
    </>
  );
}
