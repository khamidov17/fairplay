import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { scanApi, type ScanExamSummary } from "../../../api/scan";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText } from "../../../components/teacher/util";
import "../../../styles/scan.css";
import "../../../styles/scanAi.css";
import { ExportButtons } from "./ExportButtons";

export function nextStepFor(e: ScanExamSummary): { label: string; step: string } {
  if (!e.printed) return { label: "Print booklets", step: "print" };
  if (e.batches === 0) return { label: "Upload scans", step: "upload" };
  if (e.unmatched > 0) return { label: `Fix ${e.unmatched} pages`, step: "upload" };
  if (e.to_review > 0) return { label: `Review ${e.to_review} answers`, step: "grade" };
  if (!e.grades_released) return { label: "Release grades", step: "release" };
  return { label: "Open", step: "grade" };
}

/** /teacher/scan — paper exams and where each one stands. */
export default function ScanOverviewPage() {
  const [list, setList] = useState<ScanExamSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    scanApi.exams().then(
      (l) => alive && setList(l),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, []);

  return (
    <>
      <header className="t-page-head">
        <div className="t-head-text">
          <h1>Scan &amp; Grade</h1>
          <p>Students write on paper. FairPlay prints the booklets, reads the scans, and AI drafts grades for you to approve.</p>
        </div>
        <div className="t-head-actions">
          <Link to="/teacher/exams/new" className="btn btn-primary">
            New exam
          </Link>
        </div>
      </header>
      <ol className="sc-how" aria-label="How it works">
        <li>
          <strong>1. Print</strong> one booklet per student, each page with a code.
        </li>
        <li>
          <strong>2. Scan</strong> all papers on the copier and upload one PDF.
        </li>
        <li>
          <strong>3. Approve</strong> AI-drafted grades — nothing reaches students without you.
        </li>
        <li>
          <strong>4. Release</strong> as usual (gradebook and LMS included).
        </li>
      </ol>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <section className="t-card" aria-labelledby="sc-ov-h">
        <h2 id="sc-ov-h">Paper exams</h2>
        {list === null && !error ? (
          <Loading label="Loading…" />
        ) : !list || list.length === 0 ? (
          <div className="t-empty">
            <strong>No paper exams yet</strong>
            Create an exam and choose “On paper” as the way students answer.
          </div>
        ) : (
          <div className="t-table-scroll">
            <table className="t-table" style={{ minWidth: 640 }}>
              <thead>
                <tr>
                  <th scope="col">Exam</th>
                  <th scope="col">Course</th>
                  <th scope="col" className="num">Scanned</th>
                  <th scope="col">Status</th>
                  <th scope="col">
                    <span className="sr-only">Next step</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((e) => {
                  const next = nextStepFor(e);
                  return (
                    <tr key={e.exam_id}>
                      <td>
                        <Link className="t-row-link" to={`/teacher/exams/${e.exam_id}/scan`}>
                          {e.title}
                        </Link>
                      </td>
                      <td>{e.course || <span className="t-muted">—</span>}</td>
                      <td className="num">{e.paper_attempts}</td>
                      <td>
                        {e.grades_released ? (
                          <Pill tone="blue">Released</Pill>
                        ) : e.to_review > 0 ? (
                          <Pill tone="orange">{e.to_review} to review</Pill>
                        ) : e.unmatched > 0 ? (
                          <Pill tone="orange">{e.unmatched} pages to fix</Pill>
                        ) : e.printed ? (
                          <Pill tone="grey">{e.batches > 0 ? "Graded" : "Printed"}</Pill>
                        ) : (
                          <Pill tone="grey">Not printed</Pill>
                        )}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <span className="ex-row">
                          {e.paper_attempts > 0 && <ExportButtons examId={e.exam_id} />}
                          <Link to={`/teacher/exams/${e.exam_id}/scan?step=${next.step}`} className="t-btn-outline">
                            {next.label}
                          </Link>
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
    </>
  );
}
