import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { teacherApi } from "../../api/client";
import type { Exam, ExamStatus } from "../../api/types";
import { Loading } from "../../components/Loading";
import { ExamsTable } from "../../components/teacher/ExamsTable";
import { errorText } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";

type Filter = "all" | ExamStatus;
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "live", label: "Live" },
  { id: "draft", label: "Drafts" },
  { id: "closed", label: "Closed" },
];

export default function ExamsPage() {
  const [exams, setExams] = useState<Exam[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  useEffect(() => {
    let alive = true;
    teacherApi.listExams().then(
      (list) => alive && setExams(list),
      (e) => {
        if (!alive) return;
        setError(errorText(e));
        setExams([]);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const shown = (exams ?? []).filter((e) => filter === "all" || e.status === filter);

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Exams</h1>
          <p>Everything you have created, newest first.</p>
        </div>
        <Link to="/teacher/exams/new" className="btn btn-primary">
          <Icon name="plus" size={18} />
          Create exam
        </Link>
      </header>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <section className="t-card" aria-labelledby="all-exams-h">
        <div className="t-card-head">
          <h2 id="all-exams-h">All exams</h2>
          <div className="t-tabs" role="group" aria-label="Filter exams" style={{ borderBottom: "none" }}>
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={filter === f.id}
                className="t-tab"
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
        {exams === null ? (
          <Loading label="Loading exams…" />
        ) : shown.length === 0 ? (
          <div className="t-empty">
            <strong>{exams.length === 0 ? "No exams yet" : "Nothing here"}</strong>
            {exams.length === 0 ? "Create your first exam to get started." : "No exams match this filter."}
          </div>
        ) : (
          <ExamsTable exams={shown} caption="Exams" />
        )}
      </section>
    </>
  );
}
