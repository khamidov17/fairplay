import { Link } from "react-router-dom";
import type { Exam } from "../../api/types";
import { Pill } from "./TeacherLayout";
import { examStatus, formatWhen, plural } from "./util";

/** Exams table like the design's "Upcoming exams" card. */
export function ExamsTable({ exams, caption }: { exams: Exam[]; caption?: string }) {
  return (
    <div className="t-table-scroll">
      <table className="t-table" style={{ minWidth: 560 }}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            <th scope="col">Exam</th>
            <th scope="col">Course</th>
            <th scope="col">When</th>
            <th scope="col">Questions</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {exams.map((exam) => {
            const st = examStatus(exam);
            return (
              <tr key={exam.id}>
                <td>
                  <Link className="t-row-link" to={`/teacher/exams/${exam.id}`}>
                    {exam.title}
                  </Link>
                </td>
                <td>{exam.course || <span className="t-muted">—</span>}</td>
                <td>
                  {exam.scheduled_at ? (
                    formatWhen(exam.scheduled_at)
                  ) : (
                    <span className="t-muted">Created {formatWhen(exam.created_at)}</span>
                  )}
                </td>
                <td className="t-muted">
                  {plural(exam.question_count, "question")} · {exam.duration_min} min
                </td>
                <td>
                  <Pill tone={st.tone} live={st.live}>
                    {st.label}
                  </Pill>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
