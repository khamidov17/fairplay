import { useCallback, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, teacherApi } from "../../../api/client";
import type { Exam } from "../../../api/types";
import { Loading } from "../../../components/Loading";
import { errorText } from "../../../components/teacher/util";
import { ScanSteps, isScanStep, type ScanStep } from "./ScanSteps";

/** /teacher/exams/:id/scan */
export default function ScanExamPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [exam, setExam] = useState<Exam | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    teacherApi.getExam(id).then(setExam, (e) =>
      setError(e instanceof ApiError && e.status === 404 ? "This exam does not exist." : errorText(e)),
    );
  }, [id]);
  useEffect(load, [load]);

  if (error)
    return (
      <>
        <div className="alert alert-error" role="alert">
          {error}
        </div>
        <Link to="/teacher/scan" className="t-back">
          ← Scan &amp; Grade
        </Link>
      </>
    );
  if (!exam) return <Loading label="Loading exam…" />;

  const raw = params.get("step");
  const step: ScanStep = isScanStep(raw) ? raw : "print";

  return (
    <>
      <Link to="/teacher/scan" className="t-back">
        ← Scan &amp; Grade
      </Link>
      <header className="t-page-head">
        <div className="t-head-text">
          <span className="t-eyebrow">{exam.course && <span className="t-course-chip">{exam.course}</span>}</span>
          <h1>{exam.title}</h1>
          <p>Paper exam · print, scan, let AI draft the grades, approve, release.</p>
        </div>
        <div className="t-head-actions">
          <Link to={`/teacher/exams/${exam.id}`} className="btn btn-secondary">
            Exam page
          </Link>
        </div>
      </header>
      <ScanSteps exam={exam} step={step} onStep={(s) => setParams({ step: s }, { replace: true })} onExamChanged={load} />
    </>
  );
}
