// The marks grid (students × exams), used on the Grades page and a class's Marks tab.
import { Link } from "react-router-dom";
import type { Gradebook, GradebookState } from "../../api/types";
import { num, pct } from "./util";

export const STATE_LABEL: Record<GradebookState, string> = {
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

export function MarksLegend() {
  return (
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
  );
}

export function MarksTable({ data, busy }: { data: Gradebook; busy?: boolean }) {
  const { exams, students } = data;
  return (
    <section aria-label="Marks" className="t-gb-wrap" aria-busy={busy}>
      <table className="t-table t-gb" style={{ minWidth: Math.max(560, 220 + exams.length * 140) }}>
        <thead>
          <tr>
            <th scope="col">Student</th>
            {exams.map((e) => (
              <th scope="col" key={e.exam_id}>
                <Link to={`/teacher/exams/${encodeURIComponent(e.exam_id)}?tab=results`} className="t-row-link" style={{ fontWeight: 600 }}>
                  {e.title}
                </Link>
                <span className="t-gb-course">
                  {[`${e.total_points} pts`, e.grades_released ? "released" : null].filter(Boolean).join(" · ")}
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
                  (cell && cell.score != null
                    ? `${num(cell.score)} / ${e.total_points} points${state === "needs_grading" ? " so far" : ""}`
                    : STATE_LABEL[state]) + (cell?.late ? " · handed in late" : "");
                return (
                  <td key={e.exam_id} className="cell">
                    <span className={`t-cell ${state}`} title={title}>
                      {cellText(cell)}
                      {cell?.late && <span className="t-gb-late" aria-label="handed in late"> late</span>}
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
  );
}

/** Save the marks as CSV (whole gradebook or one class). */
export async function downloadCsv(blob: Blob, filename: string): Promise<void> {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
