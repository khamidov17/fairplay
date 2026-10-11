// /teacher/classes/:classId — one class: join code, students (add / remove), exams.
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, lmsApi } from "../../../api/client";
import {
  classesApi,
  countEntries,
  type AddMembersResult,
  type ClassDetail,
  type ClassStudent,
  type ClassSummary,
} from "../../../api/classes";
import type { LmsCourseLink } from "../../../api/types";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, examStatus, plural } from "../../../components/teacher/util";
import { Switch } from "../../../components/ui/Switch";
import "../../../styles/classes.css";

/** File contents as text (FileReader: works in older WebKit and in jsdom too). */
function readText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result ?? ""));
    r.onerror = () => reject(r.error);
    r.readAsText(file);
  });
}

const ADDED_BY: Record<string, string> = { teacher: "Added by you", code: "Joined with code", lms: "From LMS" };

/** "Added 2 students. 1 was already in the class. 1 not found." */
export function addSummary(r: Pick<AddMembersResult, "added" | "already" | "not_found">): string {
  const parts = [`Added ${plural(r.added.length, "student")}.`];
  if (r.already.length) parts.push(`${r.already.length} ${r.already.length === 1 ? "was" : "were"} already in the class.`);
  if (r.not_found.length) parts.push(`${r.not_found.length} not found.`);
  return parts.join(" ");
}

export default function ClassPage() {
  const { classId = "" } = useParams();
  const id = Number(classId);
  const [cls, setCls] = useState<ClassDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const reload = () =>
    classesApi.get(id).then(
      (c) => {
        setCls(c);
        setLoadError(null);
      },
      (e) => setLoadError(e instanceof ApiError && e.status === 404 ? "This class does not exist." : errorText(e)),
    );

  useEffect(() => {
    setCls(null);
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (loadError)
    return (
      <>
        <div className="alert alert-error" role="alert">
          {loadError}
        </div>
        <Link to="/teacher/classes" className="t-back">
          ← All classes
        </Link>
      </>
    );
  if (!cls) return <Loading label="Loading class…" />;

  const merge = (s: ClassSummary) => setCls((c) => (c ? { ...c, ...s } : c));
  async function patch(fn: () => Promise<ClassSummary>) {
    setActionError(null);
    try {
      merge(await fn());
    } catch (e) {
      setActionError(errorText(e));
    }
  }

  return (
    <>
      <Link to="/teacher/classes" className="t-back">
        ← All classes
      </Link>
      <ClassHeader cls={cls} onSaved={merge} onArchive={(v) => patch(() => classesApi.update(cls.id, { archived: v }))} />
      {actionError && (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      )}
      {cls.archived && (
        <div className="alert alert-info">This class is archived. Students cannot join it, and it is hidden from their class list.</div>
      )}

      <div className="cl-layout">
        <div className="cl-side" style={{ minWidth: 0 }}>
          <AddStudents classId={cls.id} onAdded={() => void reload()} />
          <StudentsTable cls={cls} onRemoved={(uid) => setCls((c) => c && { ...c, students: c.students.filter((s) => s.id !== uid), member_count: c.member_count - 1 })} />
        </div>
        <aside className="cl-side">
          <JoinCodeCard
            cls={cls}
            onRegenerate={() => patch(() => classesApi.regenerateCode(cls.id))}
            onToggle={(v) => patch(() => classesApi.update(cls.id, { join_enabled: v }))}
          />
          <section className="t-card" aria-labelledby="cl-exams-h">
            <div className="t-card-head">
              <h2 id="cl-exams-h">Exams</h2>
            </div>
            {cls.exams.length === 0 ? (
              <p className="t-small t-muted" style={{ margin: 0 }}>
                No exams for this class yet.
              </p>
            ) : (
              <div>
                {cls.exams.map((e) => {
                  const st = examStatus({ status: e.status, grades_released: false, pending_grading: 0 });
                  return (
                    <div key={e.id} className="cl-exam-row">
                      <Link to={`/teacher/exams/${e.id}`}>{e.title}</Link>
                      <Pill tone={st.tone} live={st.live}>
                        {st.label}
                      </Pill>
                    </div>
                  );
                })}
              </div>
            )}
            <Link to={`/teacher/exams/new?class=${cls.id}`} className="btn btn-primary">
              New exam for this class
            </Link>
          </section>
        </aside>
      </div>
    </>
  );
}

function ClassHeader({
  cls,
  onSaved,
  onArchive,
}: {
  cls: ClassDetail;
  onSaved: (c: ClassSummary) => void;
  onArchive: (archived: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(cls.name);
  const [section, setSection] = useState(cls.section);
  const [term, setTerm] = useState(cls.term);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError("Give the class a name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSaved(await classesApi.update(cls.id, { name: name.trim(), section: section.trim(), term: term.trim() }));
      setEditing(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  if (editing)
    return (
      <form className="t-card" onSubmit={save} aria-label="Rename class">
        <div className="cl-new">
          <div className="t-field">
            <label htmlFor="cl-edit-name">Class name</label>
            <input id="cl-edit-name" className="input" value={name} maxLength={120} autoFocus onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="t-field">
            <label htmlFor="cl-edit-section">Section or course code</label>
            <input id="cl-edit-section" className="input" value={section} maxLength={64} onChange={(e) => setSection(e.target.value)} />
          </div>
          <div className="t-field">
            <label htmlFor="cl-edit-term">Term</label>
            <input id="cl-edit-term" className="input" value={term} maxLength={64} onChange={(e) => setTerm(e.target.value)} />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => setEditing(false)}>
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

  return (
    <header className="t-page-head">
      <div className="t-head-text">
        {(cls.section || cls.term) && (
          <span className="t-eyebrow">
            {cls.section && <span className="t-course-chip">{cls.section}</span>}
            {cls.term && <span className="t-small t-muted">{cls.term}</span>}
          </span>
        )}
        <h1>{cls.name}</h1>
        <p>
          {plural(cls.member_count, "student")} · {plural(cls.exam_count, "exam")}
        </p>
      </div>
      <div className="t-head-actions">
        <button type="button" className="btn btn-secondary" onClick={() => setEditing(true)}>
          Rename
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => onArchive(!cls.archived)}>
          {cls.archived ? "Restore class" : "Archive"}
        </button>
      </div>
    </header>
  );
}

function JoinCodeCard({
  cls,
  onRegenerate,
  onToggle,
}: {
  cls: ClassSummary;
  onRegenerate: () => Promise<void>;
  onToggle: (enabled: boolean) => Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const off = !cls.join_enabled || cls.archived;

  async function copy() {
    try {
      await navigator.clipboard.writeText(cls.join_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className="t-card cl-code-card" aria-labelledby="cl-code-h">
      <h2 id="cl-code-h">Class code</h2>
      <div className={`cl-code${off ? " off" : ""}`} aria-label={`Class code ${cls.join_code.split("").join(" ")}`}>
        {cls.join_code}
      </div>
      <p className="t-small t-muted" style={{ margin: 0 }}>
        {off
          ? "Joining is off. Students already in the class stay in it."
          : "Students open Classes in FairPlay, choose Join a class and type this code."}
      </p>
      <div className="cl-code-actions">
        <button type="button" className="t-mini-btn primary" onClick={copy} disabled={off}>
          {copied ? "Copied" : "Copy code"}
        </button>
        {!confirm ? (
          <button type="button" className="t-mini-btn" onClick={() => setConfirm(true)}>
            Regenerate
          </button>
        ) : (
          <span role="alertdialog" aria-label="Make a new code?" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <span className="t-small">The old code stops working.</span>
            <button
              type="button"
              className="t-mini-btn primary"
              onClick={() => {
                setConfirm(false);
                void onRegenerate();
              }}
            >
              Make new code
            </button>
            <button type="button" className="t-mini-btn" onClick={() => setConfirm(false)}>
              Keep
            </button>
          </span>
        )}
      </div>
      <div className="t-setting">
        <span className="t-setting-text">
          <label className="t-setting-title" htmlFor="cl-join-enabled">
            Students can join with the code
          </label>
          <span className="t-setting-sub" id="cl-join-sub">
            Turn off once everyone is in
          </span>
        </span>
        <Switch id="cl-join-enabled" checked={cls.join_enabled} onChange={(v) => void onToggle(v)} describedBy="cl-join-sub" />
      </div>
    </section>
  );
}

type AddMode = "list" | "csv" | "lms";

function AddStudents({ classId, onAdded }: { classId: number; onAdded: () => void }) {
  const [mode, setMode] = useState<AddMode>("list");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [links, setLinks] = useState<LmsCourseLink[]>([]);
  const [linkId, setLinkId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AddMembersResult | null>(null);

  useEffect(() => {
    let alive = true;
    lmsApi.linkedCourses().then(
      (l) => alive && Array.isArray(l) && setLinks(l),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);

  const modes: { id: AddMode; label: string }[] = [
    { id: "list", label: "Usernames or emails" },
    { id: "csv", label: "CSV file" },
    ...(links.length ? [{ id: "lms" as AddMode, label: "From LMS course" }] : []),
  ];
  const n = countEntries(text);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    let run: () => Promise<AddMembersResult>;
    if (mode === "list") {
      if (!n) return setError("Type at least one username or email.");
      run = () => classesApi.addMembers(classId, text);
    } else if (mode === "csv") {
      if (!file) return setError("Choose a CSV file.");
      const f = file;
      run = async () => classesApi.addCsv(classId, await readText(f));
    } else {
      if (!linkId) return setError("Choose a course.");
      run = () => classesApi.importLms(classId, Number(linkId));
    }
    setBusy(true);
    try {
      const r = await run();
      setResult(r);
      if (mode === "list") setText(r.not_found.join("\n"));
      if (r.added.length) onAdded();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="t-card" onSubmit={submit} aria-labelledby="cl-add-h">
      <div className="t-card-head">
        <h2 id="cl-add-h">Add students</h2>
        <div className="t-seg" role="group" aria-label="How to add students">
          {modes.map((m) => (
            <button
              key={m.id}
              type="button"
              className={mode === m.id ? "on" : ""}
              aria-pressed={mode === m.id}
              onClick={() => {
                setMode(m.id);
                setError(null);
              }}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {mode === "list" && (
        <div className="t-field">
          <label htmlFor="cl-add-text">One username or email per line</label>
          <textarea
            id="cl-add-text"
            className="input cl-textarea"
            value={text}
            placeholder={"alice\nbob.karimov@dukekunshan.edu.cn"}
            onChange={(e) => setText(e.target.value)}
            aria-describedby="cl-add-count"
          />
          <span id="cl-add-count" className="t-small t-muted">
            {n ? plural(n, "entry", "entries") : "Paste a list from a spreadsheet or email."}
          </span>
        </div>
      )}
      {mode === "csv" && (
        <div className="t-field">
          <label htmlFor="cl-add-csv">CSV file</label>
          <input
            id="cl-add-csv"
            type="file"
            accept=".csv,text/csv,text/plain"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <span className="t-small t-muted">
            A header row with a <strong>username</strong> or <strong>email</strong> column (a <strong>name</strong>{" "}
            column helps you spot who was not found). A plain list with one per row also works.
          </span>
        </div>
      )}
      {mode === "lms" && (
        <div className="t-field">
          <label htmlFor="cl-add-lms">Linked LMS course</label>
          <select id="cl-add-lms" className="t-select" value={linkId} onChange={(e) => setLinkId(e.target.value)}>
            <option value="">Choose a course</option>
            {links.map((l) => (
              <option key={l.id} value={String(l.id)}>
                {l.name}
                {l.code ? ` (${l.code})` : ""} · {plural(l.member_count, "student")}
              </option>
            ))}
          </select>
          <span className="t-small t-muted">Copies the roster last synced from the LMS. Re-sync it on the LMS sync page first if it changed.</span>
        </div>
      )}

      <div>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Adding…" : mode === "lms" ? "Import students" : "Add students"}
        </button>
      </div>
      {error && (
        <span className="field-error" role="alert">
          {error}
        </span>
      )}
      {result && (
        <div className="cl-result" role="status">
          <strong>{addSummary(result)}</strong>
          {result.not_found.length > 0 && (
            <>
              <span className="cl-missing">No student account matches these. Check the spelling, or ask them to register first:</span>
              <ul aria-label="Not found">
                {result.not_found.slice(0, 50).map((x) => (
                  <li key={x}>{x}</li>
                ))}
              </ul>
              {result.not_found.length > 50 && <span className="t-small t-muted">and {result.not_found.length - 50} more</span>}
            </>
          )}
        </div>
      )}
    </form>
  );
}

function StudentsTable({ cls, onRemoved }: { cls: ClassDetail; onRemoved: (userId: number) => void }) {
  const [search, setSearch] = useState("");
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return cls.students;
    return cls.students.filter((s: ClassStudent) =>
      [s.name, s.username, s.email ?? ""].some((v) => v.toLowerCase().includes(q)),
    );
  }, [cls.students, search]);

  async function remove(s: ClassStudent) {
    setError(null);
    try {
      await classesApi.removeMember(cls.id, s.id);
      onRemoved(s.id);
    } catch (e) {
      setError(errorText(e));
    }
    setConfirmId(null);
  }

  return (
    <section className="t-card" aria-labelledby="cl-students-h">
      <div className="t-card-head">
        <h2 id="cl-students-h">Students · {cls.students.length}</h2>
        <label className="t-gb-filter" htmlFor="cl-search">
          Search
          <input
            id="cl-search"
            className="t-select"
            type="search"
            placeholder="Name, username or email"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
      </div>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {cls.students.length === 0 ? (
        <div className="t-empty">
          <strong>No students yet</strong>
          Share the class code, or add students by username above.
        </div>
      ) : (
        <div className="t-table-scroll">
          <table className="t-table">
            <thead>
              <tr>
                <th scope="col">Student</th>
                <th scope="col">How</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => (
                <tr key={s.id}>
                  <td>
                    <strong>{s.name}</strong>
                    <div className="t-small t-muted">{s.email ? `${s.username} · ${s.email}` : s.username}</div>
                  </td>
                  <td className="cl-by">{ADDED_BY[s.added_by ?? "teacher"]}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {confirmId === s.id ? (
                      <>
                        <button type="button" className="t-mini-btn" onClick={() => void remove(s)}>
                          Remove {s.name}
                        </button>{" "}
                        <button type="button" className="t-mini-btn" onClick={() => setConfirmId(null)}>
                          Keep
                        </button>
                      </>
                    ) : (
                      <button type="button" className="t-mini-btn" onClick={() => setConfirmId(s.id)} aria-label={`Remove ${s.name}`}>
                        Remove
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {shown.length === 0 && (
                <tr>
                  <td colSpan={3} className="t-muted">
                    No student matches “{search}”.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
