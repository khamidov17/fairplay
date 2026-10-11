import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, teacherApi } from "../../api/client";
import type { Exam } from "../../api/types";
import { Loading } from "../../components/Loading";
import { errorText } from "../../components/teacher/util";
import { ExamEditor } from "./ExamEditor";

/** /teacher/exams/new and /teacher/exams/:id/edit */
export default function ExamSetupPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [exam, setExam] = useState<Exam | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let alive = true;
    setExam(null);
    setError(null);
    teacherApi.getExam(id).then(
      (e) => alive && setExam(e),
      (e) => alive && setError(e instanceof ApiError && e.status === 404 ? "This exam does not exist." : errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [id]);

  const onSaved = (saved: Exam, opened: boolean) =>
    navigate(`/teacher/exams/${saved.id}${opened ? "?tab=live" : ""}`, { replace: !id });

  if (!id) return <ExamEditor onSaved={onSaved} cancelTo="/teacher" />;

  if (error)
    return (
      <>
        <div className="alert alert-error" role="alert">
          {error}
        </div>
        <Link to="/teacher/exams" className="t-back">
          ← All exams
        </Link>
      </>
    );
  if (!exam) return <Loading label="Loading exam…" />;
  if (exam.status !== "draft")
    return (
      <>
        <div className="alert alert-info" role="status">
          This exam is {exam.status === "live" ? "live" : "closed"}, so its questions can no longer be edited.
        </div>
        <Link to={`/teacher/exams/${exam.id}`} className="t-back">
          ← Back to {exam.title}
        </Link>
      </>
    );
  return <ExamEditor key={exam.id} exam={exam} onSaved={onSaved} cancelTo={`/teacher/exams/${exam.id}`} />;
}
