import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { classesApi, type ExamClasses } from "../../../api/classes";
import { Icon } from "../../ui/Icon";
import { errorText, plural } from "../util";
import { ClassAssignSelect, OPEN_WARNING } from "./ClassAssignSelect";
import "../../../styles/classes.css";

/** Exam page strip: who may take this exam (assigned classes + student count), with
 *  "Change" to edit the assignment in place (allowed in any exam status). */
export function ExamClassesBar({ examId }: { examId: string }) {
  const [info, setInfo] = useState<ExamClasses | null>(null);
  const [editing, setEditing] = useState(false);
  const [ids, setIds] = useState<number[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    classesApi.examClasses(examId).then(
      (r) => alive && setInfo(r),
      () => undefined, // older server / no access: the strip just stays hidden
    );
    return () => {
      alive = false;
    };
  }, [examId]);

  if (!info) return null;

  async function save() {
    if (ids === null) return;
    setBusy(true);
    setError(null);
    try {
      setInfo(await classesApi.setExamClasses(examId, ids));
      setEditing(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (editing)
    return (
      <section className="t-card" aria-label="Assign to classes" style={{ gap: 12 }}>
        <ClassAssignSelect examId={examId} value={ids} onChange={(v) => setIds(v)} />
        {error && (
          <span className="field-error" role="alert">
            {error}
          </span>
        )}
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" className="btn btn-primary" onClick={save} disabled={busy || ids === null}>
            {busy ? "Saving…" : "Save classes"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </div>
      </section>
    );

  const change = (
    <button
      type="button"
      className="t-link-btn"
      onClick={() => {
        setIds(info.classes.map((c) => c.id));
        setEditing(true);
      }}
    >
      {info.classes.length ? "Change" : "Assign to classes"}
    </button>
  );

  return (
    <div className={info.audience === "open" ? "cl-warn" : "cl-bar"} role="note" aria-label="Who can take this exam">
      {info.audience === "open" ? (
        <>
          <Icon name="info" size={18} />
          <span style={{ flex: 1 }}>{OPEN_WARNING}</span>
        </>
      ) : (
        <>
          <span className="cl-bar-label">{info.classes.length ? "Classes" : "Who can take it"}</span>
          {info.classes.map((c) => (
            <Link key={c.id} to={`/teacher/classes/${c.id}`} className="cl-chip">
              {c.name}
              {c.section ? ` · ${c.section}` : ""}
            </Link>
          ))}
          {info.classes.length > 0 && <span className="t-muted">{plural(info.student_count, "student")}</span>}
          {info.audience === "roster" && (
            <span className="t-muted">
              {info.classes.length ? "Your picked student list overrides the classes." : "Only the students you picked."}
            </span>
          )}
          {info.audience === "course" && <span className="t-muted">Only students of the linked LMS course.</span>}
        </>
      )}
      {change}
    </div>
  );
}
