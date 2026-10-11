// /teacher/classes: the teacher's classes as tiles, plus "New class".
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { classesApi, type ClassSummary } from "../../../api/classes";
import { Loading } from "../../../components/Loading";
import { errorText, plural } from "../../../components/teacher/util";
import "../../../styles/classes.css";

export default function ClassesPage() {
  const navigate = useNavigate();
  const [archived, setArchived] = useState(false);
  const [rows, setRows] = useState<ClassSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let alive = true;
    setRows(null);
    setError(null);
    classesApi.list(archived).then(
      (r) => alive && setRows(r),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [archived]);

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Classes</h1>
          <p>Put students in a class once, then give an exam to the whole class.</p>
        </div>
        <div className="t-head-actions">
          <div className="t-seg" role="group" aria-label="Which classes">
            <button type="button" className={archived ? "" : "on"} aria-pressed={!archived} onClick={() => setArchived(false)}>
              Active
            </button>
            <button type="button" className={archived ? "on" : ""} aria-pressed={archived} onClick={() => setArchived(true)}>
              Archived
            </button>
          </div>
          {!creating && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              New class
            </button>
          )}
        </div>
      </header>

      {creating && (
        <NewClassForm
          onCancel={() => setCreating(false)}
          onCreated={(c) => navigate(`/teacher/classes/${c.id}`)}
        />
      )}

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      {!rows && !error ? (
        <Loading label="Loading classes…" />
      ) : rows && rows.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>{archived ? "No archived classes" : "No classes yet"}</strong>
            {archived
              ? "Classes you archive show up here."
              : "Create a class, then share its code with students or add them by username."}
          </div>
        </div>
      ) : rows ? (
        <ul className="cl-grid" aria-label={archived ? "Archived classes" : "Classes"}>
          {rows.map((c) => (
            <li key={c.id}>
              <Link to={`/teacher/classes/${c.id}`} className={`cl-tile${c.archived ? " archived" : ""}`}>
                <span className="cl-tile-name">{c.name}</span>
                {(c.section || c.term) && (
                  <span className="cl-tile-sub">{[c.section, c.term].filter(Boolean).join(" · ")}</span>
                )}
                <span className="cl-tile-foot">
                  <span>{plural(c.member_count, "student")}</span>
                  <span>{plural(c.exam_count, "exam")}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

function NewClassForm({ onCancel, onCreated }: { onCancel: () => void; onCreated: (c: ClassSummary) => void }) {
  const [name, setName] = useState("");
  const [section, setSection] = useState("");
  const [term, setTerm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError("Give the class a name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onCreated(await classesApi.create({ name: name.trim(), section: section.trim(), term: term.trim() }));
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <form className="t-card" onSubmit={submit} aria-labelledby="new-class-h">
      <h2 id="new-class-h">New class</h2>
      <div className="cl-new">
        <div className="t-field">
          <label htmlFor="cl-name">Class name</label>
          <input
            id="cl-name"
            className="input"
            value={name}
            maxLength={120}
            placeholder="e.g. Calculus I"
            autoFocus
            onChange={(e) => setName(e.target.value)}
            aria-invalid={!!error}
          />
        </div>
        <div className="t-field">
          <label htmlFor="cl-section">Section or course code</label>
          <input id="cl-section" className="input" value={section} maxLength={64} placeholder="MATH 101-02" onChange={(e) => setSection(e.target.value)} />
        </div>
        <div className="t-field">
          <label htmlFor="cl-term">Term</label>
          <input id="cl-term" className="input" value={term} maxLength={64} placeholder="Fall 2026" onChange={(e) => setTerm(e.target.value)} />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Creating…" : "Create class"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
      {error && (
        <span className="field-error" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}
