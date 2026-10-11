import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import type { Exam } from "../../../api/types";
import "../../../styles/scan.css";
import { PrintStep } from "./PrintStep";
import { QueueStep } from "./QueueStep";
import { UploadStep } from "./UploadStep";

export type ScanStep = "print" | "upload" | "grade" | "release";

export const SCAN_STEPS: { id: ScanStep; n: number; label: string; hint: string }[] = [
  { id: "print", n: 1, label: "Print", hint: "Booklets with codes" },
  { id: "upload", n: 2, label: "Upload scans", hint: "Match pages to students" },
  { id: "grade", n: 3, label: "AI grading", hint: "Approve or fix drafts" },
  { id: "release", n: 4, label: "Release", hint: "Students see grades" },
];

export function isScanStep(s: string | null): s is ScanStep {
  return SCAN_STEPS.some((x) => x.id === s);
}

/** The four Scan & Grade steps for one exam (used on /teacher/exams/:id/scan and in the
 *  exam page for paper exams). */
export function ScanSteps({
  exam,
  step,
  onStep,
  onExamChanged,
}: {
  exam: Exam;
  step?: ScanStep;
  onStep?: (s: ScanStep) => void;
  onExamChanged?: () => void;
}) {
  const [local, setLocal] = useState<ScanStep>("print");
  const current = step ?? local;
  const go = (s: ScanStep) => (onStep ? onStep(s) : setLocal(s));
  const changed = useCallback(() => onExamChanged?.(), [onExamChanged]);

  return (
    <div className="sc-steps">
      <ol className="sc-stepper" aria-label="Scan and grade steps">
        {SCAN_STEPS.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              className="sc-stepbtn"
              aria-current={current === s.id ? "step" : undefined}
              onClick={() => go(s.id)}
            >
              <span className="sc-stepnum" aria-hidden="true">
                {s.n}
              </span>
              <span className="sc-steptext">
                <span className="sc-steplabel">{s.label}</span>
                <span className="sc-stephint">{s.hint}</span>
              </span>
            </button>
          </li>
        ))}
      </ol>

      <div className="sc-panel">
        {current === "print" && <PrintStep examId={exam.id} title={exam.title} />}
        {current === "upload" && <UploadStep examId={exam.id} onChanged={changed} />}
        {current === "grade" && <QueueStep examId={exam.id} onChanged={changed} />}
        {current === "release" && (
          <section className="t-card sc-release" aria-labelledby="sc-rel-h">
            <h3 id="sc-rel-h">Release grades</h3>
            {exam.grades_released ? (
              <p>Grades are released. Students see their scores, your comments and the picture of their answers.</p>
            ) : (exam.pending_grading ?? 0) > 0 ? (
              <p>
                {exam.pending_grading} answers still wait for your approval. Approve them in step 3, then release from
                Results.
              </p>
            ) : (
              <p>All answers are graded. Check the results, then release them to students (and send them to your LMS).</p>
            )}
            <div className="sc-inline">
              <Link to={`/teacher/exams/${exam.id}?tab=results`} className="btn btn-primary">
                Open Results &amp; Release
              </Link>
              {(exam.pending_grading ?? 0) > 0 && (
                <button type="button" className="t-btn-outline" onClick={() => go("grade")}>
                  Back to AI grading
                </button>
              )}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
