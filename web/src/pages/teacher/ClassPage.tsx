// 2026-10-13: one class, like a Google Classroom class page: banner (name, term, teacher,
// students), the code and join link, and three tabs: Exams · Students · Marks.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { classesApi, teacherApi } from "../../api/client";
import type { AddMembersResult, ClassDetail, Exam, Gradebook } from "../../api/types";
import { Loading } from "../../components/Loading";
import { MarksLegend, MarksTable, downloadCsv } from "../../components/teacher/MarksTable";
import { Pill } from "../../components/teacher/TeacherLayout";
import { classColor, copyText, joinLink } from "../../components/teacher/classColor";
import { errorText, examStatus, plural } from "../../components/teacher/util";
import { Icon } from "../../components/ui/Icon";

type Tab = "exams" | "students" | "marks";
const TABS: { id: Tab; label: string }[] = [
  { id: "exams", label: "Exams" },
  { id: "students", label: "Students" },
  { id: "marks", label: "Marks" },
];

export default function ClassPage() {
  const { classId = "" } = useParams();
  const id = Number(classId);
  const [params, setParams] = useSearchParams();
  const tab: Tab = (TABS.find((t) => t.id === params.get("tab"))?.id ?? "exams") as Tab;
  const [k, setK] = useState<ClassDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<"code" | "link" | null>(null);
  const [editing, setEditing] = useState(false);

  const load = useCallback(async () => {
    try {
      setK(await classesApi.get(id));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  function selectTab(t: Tab) {
    const next = new URLSearchParams(params);
    next.set("tab", t);
    setParams(next, { replace: true });
  }

  async function copy(what: "code" | "link") {
    if (!k) return;
    if (await copyText(what === "code" ? k.join_code : joinLink(k.join_code))) {
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    }
  }

  if (!k) {
    return error ? (
      <div className="alert alert-error" role="alert">
        {error} <Link to="/teacher/classes">Back to classes</Link>
      </div>
    ) : (
      <Loading label="Loading class…" />
    );
  }

  return (
    <>
      <nav className="t-crumbs" aria-label="Breadcrumb">
        <Link to="/teacher/classes">Classes</Link> <span aria-hidden="true">/</span> {k.name}
      </nav>

      <header className="t-gc-hero" style={{ background: classColor(k.id) }}>
        <div>
          <h1>{k.name}</h1>
          <p>{[k.term, k.teacher_name, plural(k.member_count, "student")].filter(Boolean).join(" · ")}</p>
        </div>
        <button type="button" className="t-gc-hero-btn" onClick={() => setEditing((v) => !v)} aria-expanded={editing}>
          Edit class
        </button>
      </header>

      {editing && <EditClass k={k} onDone={(changed) => { setEditing(false); if (changed) void load(); }} />}

      <section className="t-gc-share" aria-label="Invite students">
        <div>
          <span className="t-dark-box-label">Class code</span>
          <span className="t-code t-code-sm">{k.join_code}</span>
          <button type="button" className="t-mini-link" onClick={() => void copy("code")}>
            {copied === "code" ? "copied" : "copy"}
          </button>
        </div>
        <button type="button" className="t-btn-outline" onClick={() => void copy("link")}>
          <Icon name={copied === "link" ? "check" : "link"} size={18} />
          {copied === "link" ? "Link copied" : "Copy invite link"}
        </button>
        <span className="t-small t-muted">Students open the link (or type the code after signing in) to join.</span>
      </section>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <div className="t-tabs" role="tablist" aria-label="Class sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`ctab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`cpanel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            className="t-tab"
            onClick={() => selectTab(t.id)}
            onKeyDown={(ev) => {
              const i = TABS.findIndex((x) => x.id === t.id);
              if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
                ev.preventDefault();
                const next = TABS[(i + (ev.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
                if (!next) return;
                selectTab(next.id);
                document.getElementById(`ctab-${next.id}`)?.focus();
              }
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`cpanel-${tab}`} aria-labelledby={`ctab-${tab}`} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {tab === "exams" && <ClassExams classId={k.id} />}
        {tab === "students" && <ClassStudents k={k} onChanged={load} />}
        {tab === "marks" && <ClassMarks classId={k.id} name={k.name} />}
      </div>
    </>
  );
}

function ClassExams({ classId }: { classId: number }) {
  const [exams, setExams] = useState<Exam[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    teacherApi.listExams().then(
      (all) => alive && setExams(all.filter((e) => e.class_id === classId)),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [classId]);

  return (
    <section className="t-card" aria-labelledby="cx-h" style={{ gap: 12 }}>
      <div className="t-card-head">
        <h2 id="cx-h">Exams</h2>
        <Link to={`/teacher/exams/new?class=${classId}`} className="btn btn-primary">
          <Icon name="plus" size={18} />
          New exam
        </Link>
      </div>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {exams === null && !error ? (
        <Loading label="Loading exams…" />
      ) : !exams || exams.length === 0 ? (
        <div className="t-empty">
          <strong>No exams for this class yet</strong>
          Press “New exam”, or pick this class when you edit an exam you already wrote.
        </div>
      ) : (
        <ul className="t-cx-list">
          {exams.map((e) => {
            const st = examStatus(e);
            const action = e.status === "live" ? "Live room" : e.status === "draft" ? "Edit" : "Results";
            const to =
              e.status === "draft"
                ? `/teacher/exams/${encodeURIComponent(e.id)}/edit`
                : `/teacher/exams/${encodeURIComponent(e.id)}${e.status === "live" ? "" : "?tab=results"}`;
            return (
              <li key={e.id} className="t-cx-row">
                <div style={{ minWidth: 0 }}>
                  <Link to={`/teacher/exams/${encodeURIComponent(e.id)}`} className="t-row-link" style={{ fontWeight: 600 }}>
                    {e.title || "Untitled exam"}
                  </Link>
                  <div className="t-small t-muted">
                    {plural(e.question_count, "question")} · {e.duration_min} min
                  </div>
                </div>
                <Pill tone={st.tone} live={st.live}>
                  {st.label}
                </Pill>
                <Link to={to} className="t-btn-outline">
                  {action}
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ClassStudents({ k, onChanged }: { k: ClassDetail; onChanged: () => Promise<void> | void }) {
  const [names, setNames] = useState("");
  const [result, setResult] = useState<AddMembersResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(kind: string, fn: () => Promise<unknown>) {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      await onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    const list = names.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
    if (list.length === 0) return;
    await run("add", async () => {
      setResult(await classesApi.addMembers(k.id, list));
      setNames("");
    });
  }

  return (
    <section className="t-card" aria-labelledby="cs-h" style={{ gap: 12 }}>
      <h2 id="cs-h">Students · {k.member_count}</h2>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <form onSubmit={add} className="t-class-add">
        <label htmlFor={`class-${k.id}-names`} className="t-small">
          Add students by username (one per line, or separated by commas)
        </label>
        <textarea
          id={`class-${k.id}-names`}
          className="input"
          rows={2}
          value={names}
          onChange={(e) => setNames(e.target.value)}
          placeholder={"alice\nbob"}
        />
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button type="submit" className="btn btn-secondary" disabled={busy !== null || !names.trim()}>
            {busy === "add" ? "Adding…" : "Add students"}
          </button>
          {result && (
            <span className="t-small" role="status">
              Added {result.added}
              {result.already ? `, ${result.already} already in the class` : ""}
              {result.unknown.length ? `. Not found: ${result.unknown.join(", ")}` : ""}
            </span>
          )}
        </div>
      </form>
      {k.members.length === 0 ? (
        <p className="t-muted" style={{ margin: 0 }}>
          No students yet. Share the invite link or read out the class code.
        </p>
      ) : (
        <ul className="t-cx-list">
          {k.members.map((m) => (
            <li key={m.username} className="t-cx-row">
              <span className="t-avatar" aria-hidden="true">
                {(m.name || m.username).trim().charAt(0).toUpperCase()}
              </span>
              <div style={{ minWidth: 0, flex: 1 }}>
                <Link to={`/teacher/students/${encodeURIComponent(m.username)}`} className="t-row-link" style={{ fontWeight: 600 }}>
                  {m.name}
                </Link>
                <div className="t-small t-muted">
                  {m.username} · joined {new Date(m.joined_at).toLocaleDateString()}
                </div>
              </div>
              <button
                type="button"
                className="t-mini-link"
                disabled={busy !== null}
                onClick={() => void run(`rm:${m.username}`, () => classesApi.removeMember(k.id, m.username))}
                aria-label={`Remove ${m.name} from the class`}
              >
                {busy === `rm:${m.username}` ? "removing…" : "remove"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ClassMarks({ classId, name }: { classId: number; name: string }) {
  const [data, setData] = useState<Gradebook | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  useEffect(() => {
    let alive = true;
    teacherApi.gradebook("", classId).then(
      (g) => alive && setData(g),
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [classId]);

  async function exportCsv() {
    setExporting(true);
    try {
      const safe = name.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "class";
      await downloadCsv(await teacherApi.gradebookCsv("", classId), `fairplay-${safe}-marks.csv`);
    } catch (e) {
      setError(errorText(e, "Could not download the CSV. Try again."));
    } finally {
      setExporting(false);
    }
  }

  return (
    <section className="t-card" aria-labelledby="cm-h" style={{ gap: 12 }}>
      <div className="t-card-head">
        <h2 id="cm-h">Marks</h2>
        <span style={{ display: "inline-flex", gap: 8 }}>
          <Link to="/teacher/gradebook" className="t-btn-outline">
            Grade and release
          </Link>
          <button type="button" className="t-btn-outline" onClick={exportCsv} disabled={exporting || !data}>
            <Icon name="download" size={18} />
            {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </span>
      </div>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {!data && !error ? (
        <Loading label="Loading marks…" />
      ) : !data ? null : data.exams.length === 0 || data.students.length === 0 ? (
        <div className="t-empty">
          <strong>{data.exams.length === 0 ? "No opened exams yet" : "No students yet"}</strong>
          {data.exams.length === 0 ? "Marks appear here once this class takes an exam." : "Add students first."}
        </div>
      ) : (
        <>
          <MarksLegend />
          <MarksTable data={data} />
        </>
      )}
    </section>
  );
}

function EditClass({ k, onDone }: { k: ClassDetail; onDone: (changed: boolean) => void }) {
  const navigate = useNavigate();
  const [name, setName] = useState(k.name);
  const [term, setTerm] = useState(k.term ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  async function run(kind: string, fn: () => Promise<unknown>, after: () => void) {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      after();
    } catch (e) {
      setError(errorText(e));
      setBusy(null);
    }
  }

  return (
    <form
      className="t-card t-class-new"
      aria-label="Edit class"
      onSubmit={(e) => {
        e.preventDefault();
        if (!name.trim()) return;
        void run("save", () => classesApi.update(k.id, name.trim(), term.trim()), () => onDone(true));
      }}
    >
      {error && (
        <div className="alert alert-error" role="alert" style={{ gridColumn: "1 / -1" }}>
          {error}
        </div>
      )}
      <div className="t-field">
        <label htmlFor="class-edit-name">Class name</label>
        <input id="class-edit-name" className="input" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="t-field">
        <label htmlFor="class-edit-term">Term or section</label>
        <input
          id="class-edit-term"
          className="input"
          value={term}
          maxLength={80}
          placeholder="e.g. Fall 2026 · Section 2"
          onChange={(e) => setTerm(e.target.value)}
        />
      </div>
      <div className="t-class-new-actions">
        <button
          type="button"
          className="t-btn-outline"
          disabled={busy !== null}
          title="The old code and link stop working. Students already in the class stay."
          onClick={() => void run("code", () => classesApi.newJoinCode(k.id), () => onDone(true))}
        >
          {busy === "code" ? "Changing…" : "New code and link"}
        </button>
        {!confirmDelete ? (
          <button type="button" className="btn btn-danger-outline" disabled={busy !== null} onClick={() => setConfirmDelete(true)}>
            Delete class
          </button>
        ) : (
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className="t-small">Delete “{k.name}”? Its exams must be moved or deleted first.</span>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy !== null}
              onClick={() => void run("delete", () => classesApi.remove(k.id), () => navigate("/teacher/classes"))}
            >
              Yes, delete
            </button>
            <button type="button" className="t-btn-outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </button>
          </span>
        )}
        <span style={{ flex: 1 }} />
        <button type="button" className="t-btn-outline" onClick={() => onDone(false)}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={busy !== null || !name.trim()}>
          {busy === "save" ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}
