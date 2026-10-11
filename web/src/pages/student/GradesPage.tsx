import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, studentApi } from "../../api/client";
import type { StudentGradeSummary } from "../../api/types";
import { fmtNum, fmtPercent, shortDate } from "../../components/student/format";
import { StudentLayout, StuLoading } from "../../components/student/StudentLayout";

const NO_COURSE = "Other exams";

export interface CourseGroup {
  course: string;
  items: StudentGradeSummary[];
  average: number | null;
  graded: number;
  waiting: number;
}

/** Group grades by course (in first-seen order) with the average over released exams. */
export function groupByCourse(grades: StudentGradeSummary[]): CourseGroup[] {
  const map = new Map<string, StudentGradeSummary[]>();
  for (const g of grades) {
    const key = g.course?.trim() || NO_COURSE;
    const list = map.get(key) ?? [];
    list.push(g);
    map.set(key, list);
  }
  return [...map.entries()].map(([course, items]) => {
    const released = items.filter((g) => g.released && g.percent !== null);
    const average = released.length ? released.reduce((s, g) => s + (g.percent ?? 0), 0) / released.length : null;
    return { course, items, average, graded: released.length, waiting: items.length - released.length };
  });
}

export default function GradesPage() {
  const [grades, setGrades] = useState<StudentGradeSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    studentApi.grades().then(
      (g) => alive && setGrades(g),
      (e) => {
        if (!alive) return;
        setError(e instanceof ApiError ? e.detail : "Cannot reach the server. Check the connection and reload.");
        setGrades([]);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const groups = useMemo(() => (grades ? groupByCourse(grades) : []), [grades]);
  const current = groups.find((g) => g.course === selected) ?? groups[0];

  return (
    <StudentLayout>
      <main className="stu-main" style={{ gap: 24 }}>
        <h1 className="stu-h1">Grades</h1>
        {error && (
          <div className="stu-alert error" role="alert">
            {error}
          </div>
        )}
        {grades === null ? (
          <StuLoading label="Loading grades…" />
        ) : groups.length === 0 ? (
          !error && (
            <div className="stu-card">
              <h2>No grades yet</h2>
              <p className="stu-muted">Exams you submit show up here. Scores appear when your teacher releases them.</p>
            </div>
          )
        ) : (
          <>
            <section aria-label="Courses" className="stu-course-grid">
              {groups.map((g) => (
                <button
                  key={g.course}
                  type="button"
                  className="stu-course"
                  aria-pressed={g === current}
                  onClick={() => setSelected(g.course)}
                >
                  <span className="c">{g.course}</span>
                  <span className="p">{fmtPercent(g.average)}</span>
                  <span className="n">
                    {[g.graded && `${g.graded} graded`, g.waiting && `${g.waiting} being graded`].filter(Boolean).join(" · ")}
                  </span>
                </button>
              ))}
            </section>

            {current && (
              <section className="stu-card" aria-labelledby="course-h" style={{ gap: 4 }}>
                <h2 id="course-h" style={{ marginBottom: 8 }}>
                  {current.course}
                </h2>
                <div className="stu-rows">
                  {current.items.map((g) => (
                    <GradeRow key={g.exam_id} g={g} />
                  ))}
                </div>
              </section>
            )}
            <p className="stu-hint">Scores show once your teacher releases them. Open the feedback to see every question.</p>
          </>
        )}
      </main>
    </StudentLayout>
  );
}

function GradeRow({ g }: { g: StudentGradeSummary }) {
  const inner = (
    <>
      <span className="stu-result-title">
        {g.title}
        {g.submitted_at && <small>Submitted {shortDate(g.submitted_at)}</small>}
      </span>
      {g.released ? (
        <>
          <span className="stu-result-score">
            {fmtNum(g.score)} / {fmtNum(g.total_points)}
          </span>
          <span className="stu-link">Feedback</span>
        </>
      ) : (
        <span className="stu-pill">Being graded</span>
      )}
    </>
  );
  return g.released ? (
    <Link to={`/student/grades/${g.exam_id}`} className="stu-result-row">
      {inner}
    </Link>
  ) : (
    <div className="stu-result-row">{inner}</div>
  );
}
