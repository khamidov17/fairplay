// 2026-10-13: classes as cards (like Google Classroom). Each card: name, term, teacher,
// student count; click to open the class. Students join with the code or the class link.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { classesApi } from "../../api/client";
import type { SchoolClass } from "../../api/types";
import { Loading } from "../../components/Loading";
import { classColor, copyText, joinLink } from "../../components/teacher/classColor";
import { errorText, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";

export default function ClassesPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<SchoolClass[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [term, setTerm] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await classesApi.list());
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const k = await classesApi.create(name.trim(), term.trim());
      navigate(`/teacher/classes/${k.id}?tab=students`);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  async function copyLink(k: SchoolClass) {
    if (await copyText(joinLink(k.join_code))) {
      setCopied(k.id);
      setTimeout(() => setCopied(null), 1500);
    }
  }

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Classes</h1>
          <p>Students join with the class code or link. Only a class's students see its exams.</p>
        </div>
        <div className="t-head-actions">
          <button type="button" className="btn btn-primary" onClick={() => setCreating((v) => !v)} aria-expanded={creating}>
            <Icon name="plus" size={18} />
            New class
          </button>
        </div>
      </header>

      {creating && (
        <form className="t-card t-class-new" onSubmit={create} aria-label="New class">
          <div className="t-field">
            <label htmlFor="class-new-name">Class name</label>
            <input
              id="class-new-name"
              className="input"
              placeholder="e.g. Calculus I"
              value={name}
              maxLength={120}
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="t-field">
            <label htmlFor="class-new-term">Term or section (optional)</label>
            <input
              id="class-new-term"
              className="input"
              placeholder="e.g. Fall 2026 · Section 2"
              value={term}
              maxLength={80}
              onChange={(e) => setTerm(e.target.value)}
            />
          </div>
          <div className="t-class-new-actions">
            <button type="button" className="t-btn-outline" onClick={() => setCreating(false)}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
              {busy ? "Creating…" : "Create class"}
            </button>
          </div>
        </form>
      )}

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      {rows === null && !error ? (
        <Loading label="Loading classes…" />
      ) : rows && rows.length === 0 ? (
        <div className="t-card">
          <div className="t-empty">
            <strong>No classes yet</strong>
            Press “New class”, then share the class link or code with your students.
          </div>
        </div>
      ) : rows ? (
        <ul className="t-class-grid" aria-label="Your classes">
          {rows.map((k) => (
            <li key={k.id} className="t-gc-card">
              <Link to={`/teacher/classes/${k.id}`} className="t-gc-banner" style={{ background: classColor(k.id) }}>
                <span className="t-gc-name">{k.name}</span>
                {k.term ? <span className="t-gc-term">{k.term}</span> : null}
                {k.teacher_name ? <span className="t-gc-teacher">{k.teacher_name}</span> : null}
              </Link>
              <div className="t-gc-body">
                <span>{plural(k.member_count, "student")}</span>
                <span>{plural(k.exam_count, "exam")}</span>
              </div>
              <div className="t-gc-foot">
                <span className="t-gc-code" title="Class code">
                  {k.join_code}
                </span>
                <button
                  type="button"
                  className="t-icon-btn"
                  onClick={() => void copyLink(k)}
                  aria-label={`Copy the join link for ${k.name}`}
                  title="Copy join link"
                >
                  {copied === k.id ? <Icon name="check" size={18} /> : <Icon name="link" size={18} />}
                </button>
                <Link to={`/teacher/classes/${k.id}?tab=marks`} className="t-icon-btn" aria-label={`Marks for ${k.name}`} title="Marks">
                  <Icon name="gradebook" size={18} />
                </Link>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}
