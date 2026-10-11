import { Link } from "react-router-dom";
import type { StudentExamSummary } from "../../api/types";
import { examName, fmtPercent, shortDate } from "./format";

/** One past exam: percent + "See feedback" when released, else "Being graded". */
export function PastExamRow({ exam }: { exam: StudentExamSummary }) {
  const a = exam.attempt;
  const released = !!a?.released;
  const inner = (
    <>
      <span className="stu-result-title">{examName(exam.course, exam.title)}</span>
      <span className="stu-result-date">{shortDate(a?.submitted_at)}</span>
      {released ? (
        <>
          <span className="stu-result-score">{fmtPercent(a?.percent)}</span>
          <span className="stu-link">See feedback</span>
        </>
      ) : (
        <span className="stu-pill">Being graded</span>
      )}
    </>
  );
  return released ? (
    <Link to={`/student/grades/${exam.id}`} className="stu-result-row">
      {inner}
    </Link>
  ) : (
    <div className="stu-result-row">{inner}</div>
  );
}
