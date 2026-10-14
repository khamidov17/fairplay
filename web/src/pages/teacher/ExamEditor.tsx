// 2026-10-13: Google Forms-style exam editor. Title, class and duration on top; one card per
// question (type switch inside the card); everything else under "More settings". An exam
// can be saved without a class; FairPlay asks for the class when it is opened.
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, classesApi, lmsApi, slice6Api, teacherApi } from "../../api/client";
import type { AllowedAppOption, Exam, SchoolClass, TopicCount } from "../../api/types";
import { Icon } from "../../components/ui/Icon";
import { Switch } from "../../components/ui/Switch";
import { ExternalExamSettings } from "../../components/teacher/ExternalExamSettings";
import { LmsCourseSelect } from "../../components/teacher/lms/LmsCourseSelect";
import { QuestionImagesEditor } from "../../components/teacher/QuestionImagesEditor";
import { TopicInput } from "../../components/teacher/TopicInput";
import { useFeatures } from "../../lib/features";
import {
  KIND_LABEL,
  changeKind,
  copyQuestions,
  draftFromExam,
  hasErrors,
  moveItem,
  newQuestion,
  newRubricRow,
  questionMeta,
  rubricSum,
  toExamInput,
  validateDraft,
  type DraftExam,
  type DraftKind,
  type DraftQuestion,
  type DraftRubricRow,
  type QuestionErrors,
} from "./examDraft";

const LETTERS = "ABCDEFGH";

const MENU: { kind: DraftKind; hint: string }[] = [
  { kind: "mcq", hint: "One correct option, graded automatically" },
  { kind: "tf", hint: "Graded automatically" },
  { kind: "numeric", hint: "A number with optional tolerance" },
  { kind: "short", hint: "A word or phrase from your list" },
  { kind: "essay", hint: "Written answer, you grade with a rubric" },
];

/** Bottom quick-add buttons (labels kept stable for the e2e test). */
const QUICK: { kind: DraftKind; label: string }[] = [
  { kind: "mcq", label: "+ Multiple choice" },
  { kind: "tf", label: "+ True / false" },
  { kind: "numeric", label: "+ Numeric answer" },
  { kind: "short", label: "+ Short answer" },
  { kind: "essay", label: "+ Essay" },
];

interface Props {
  exam?: Exam;
  /** Called after a successful save. `opened` is true when the teacher pressed Open exam. */
  onSaved: (exam: Exam, opened: boolean) => void;
  cancelTo: string;
}

export function ExamEditor({ exam, onSaved, cancelTo }: Props) {
  const [params] = useSearchParams();
  const features = useFeatures();
  const [draft, setDraft] = useState<DraftExam>(() => {
    const d = draftFromExam(exam);
    // "New exam" from a class page: that class is already picked.
    const fromClass = params.get("class");
    if (!exam && fromClass && /^\d+$/.test(fromClass)) d.classId = fromClass;
    return d;
  });
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [showErrors, setShowErrors] = useState(false);
  const [saving, setSaving] = useState<null | "save" | "open">(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [moreOpen, setMoreOpen] = useState(() => draft.external.mode === "external" || draft.delivery === "paper");
  const [askClass, setAskClass] = useState(false);
  const [copying, setCopying] = useState(false);
  // slice 5: linked LMS course (saved through PUT /api/exams/{id}/lms)
  const [lmsLink, setLmsLink] = useState<number | null>(exam?.course_link_id ?? null);
  // Audit W-08: once created, later saves update that exam (a failed "Open" made duplicates).
  const [created, setCreated] = useState<Exam | null>(null);
  const errors = useMemo(() => validateDraft(draft), [draft]);
  const isPaper = draft.delivery === "paper" && draft.external.mode !== "external";
  // slice 6: topic suggestions (the teacher's earlier topics); failure just means no suggestions
  const [topicSuggestions, setTopicSuggestions] = useState<TopicCount[]>([]);
  useEffect(() => {
    let alive = true;
    slice6Api.topics().then(
      (t) => alive && Array.isArray(t) && setTopicSuggestions(t),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);
  // 2026-10-14: apps a teacher may allow (everything else is closed on students' computers)
  const [appOptions, setAppOptions] = useState<AllowedAppOption[]>([]);
  useEffect(() => {
    let alive = true;
    teacherApi.allowedApps().then(
      (a) => alive && Array.isArray(a) && setAppOptions(a),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);
  const err = showErrors ? errors : { byKey: {} as Record<string, QuestionErrors> };

  useEffect(() => {
    if (!focusKey) return;
    const el = document.getElementById(`q${focusKey}-prompt`);
    if (el) {
      el.focus();
      el.scrollIntoView?.({ block: "center", behavior: "smooth" });
    }
    setFocusKey(null);
  }, [focusKey]);

  // Settings with a problem open the "More settings" panel so the error is visible.
  useEffect(() => {
    if (showErrors && (err.course || err.room || err.schedule || err.external || err.externalPoints)) setMoreOpen(true);
  }, [showErrors, err.course, err.room, err.schedule, err.external, err.externalPoints]);

  const set = (patch: Partial<DraftExam>) => setDraft((d) => ({ ...d, ...patch }));
  const updateQ = (key: string, patch: Partial<DraftQuestion>) =>
    setDraft((d) => ({ ...d, questions: d.questions.map((q) => (q.key === key ? { ...q, ...patch } : q)) }));
  const retypeQ = (key: string, kind: DraftKind) =>
    setDraft((d) => ({ ...d, questions: d.questions.map((q) => (q.key === key ? changeKind(q, kind) : q)) }));
  const addQ = (kind: DraftKind) => {
    const q = newQuestion(kind);
    setDraft((d) => ({ ...d, questions: [...d.questions, q] }));
    setOpen((o) => ({ ...o, [q.key]: true }));
    setFocusKey(q.key);
  };
  const removeQ = (key: string) => setDraft((d) => ({ ...d, questions: d.questions.filter((q) => q.key !== key) }));
  const moveQ = (i: number, delta: number) => setDraft((d) => ({ ...d, questions: moveItem(d.questions, i, delta) }));

  async function save(andOpen: boolean, classOverride?: string) {
    setShowErrors(true);
    setServerError(null);
    if (hasErrors(errors)) {
      setOpen((o) => {
        const next = { ...o };
        for (const k of Object.keys(errors.byKey)) next[k] = true;
        return next;
      });
      return;
    }
    const classId = classOverride ?? draft.classId;
    // Audit S-03: an exam opens for one class (or a linked LMS course). Saving needs neither.
    if (andOpen && !classId && lmsLink == null) {
      setAskClass(true);
      return;
    }
    setSaving(andOpen ? "open" : "save");
    try {
      const input = toExamInput({ ...draft, classId });
      const existing = exam ?? created;
      let saved = existing ? await teacherApi.updateExam(existing.id, input) : await teacherApi.createExam(input);
      if (!existing) setCreated(saved);
      if ((saved.course_link_id ?? null) !== lmsLink) saved = await lmsApi.linkExam(saved.id, lmsLink);
      if (andOpen) saved = await teacherApi.openExam(saved.id);
      onSaved(saved, andOpen);
    } catch (e) {
      if (e instanceof ApiError) setServerError(e.status === 422 ? `Please check the exam: ${e.detail}` : e.detail);
      else setServerError("Cannot reach the server. Your changes are still here; try saving again.");
      setSaving(null);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void save(false);
  }

  const totalPoints = draft.questions.reduce((s, q) => s + (Number(q.points) || 0), 0);
  const errorCount =
    (err.title ? 1 : 0) +
    (err.duration ? 1 : 0) +
    (err.questions ? 1 : 0) +
    (err.course ? 1 : 0) +
    (err.room ? 1 : 0) +
    (err.schedule ? 1 : 0) +
    Object.keys(err.byKey).length;

  return (
    <form onSubmit={onSubmit} noValidate aria-label="Exam setup" className="t-fx">
      <header className="t-fx-top">
        <span className="t-course-chip">{exam ? "Draft" : "New exam"}</span>
        <span style={{ flex: 1 }} />
        <Link to={cancelTo} className="btn btn-ghost">
          Cancel
        </Link>
        <button type="submit" className="btn btn-secondary" disabled={!!saving}>
          {saving === "save" ? "Saving…" : "Save exam"}
        </button>
        {/* slice 6: paper exams are printed, never opened in the app */}
        {!isPaper && (
          <button type="button" className="btn btn-primary" disabled={!!saving} onClick={() => void save(true)}>
            {saving === "open" ? "Opening…" : "Open exam"}
          </button>
        )}
      </header>

      {showErrors && errorCount > 0 && (
        <div className="alert alert-error" role="alert">
          Please fix the highlighted {errorCount === 1 ? "field" : "fields"} before saving.
        </div>
      )}
      {serverError && (
        <div className="alert alert-error" role="alert">
          {serverError}
        </div>
      )}

      {askClass && (
        <OpenForClass
          onCancel={() => setAskClass(false)}
          onPick={(classId) => {
            set({ classId });
            setAskClass(false);
            void save(true, classId);
          }}
        />
      )}

      <section className="t-card t-fx-head" aria-label="Exam">
        <label htmlFor="exam-title" className="sr-only">
          Exam title
        </label>
        <input
          id="exam-title"
          className="t-title-input"
          value={draft.title}
          placeholder="Untitled exam"
          maxLength={200}
          onChange={(e) => set({ title: e.target.value })}
          aria-invalid={!!err.title}
          aria-describedby={err.title ? "exam-title-err" : undefined}
        />
        {err.title && (
          <span id="exam-title-err" className="field-error">
            {err.title}
          </span>
        )}
        <div className="t-fx-basics">
          <ClassSelect value={draft.classId} onChange={(classId) => set({ classId })} />
          <div className="t-field" style={{ maxWidth: 160 }}>
            <label htmlFor="exam-duration">Duration (minutes)</label>
            <input
              id="exam-duration"
              className="input mono"
              inputMode="numeric"
              value={draft.duration}
              onChange={(e) => set({ duration: e.target.value })}
              aria-invalid={!!err.duration}
              aria-describedby={err.duration ? "exam-duration-err" : undefined}
            />
            {err.duration && (
              <span id="exam-duration-err" className="field-error">
                {err.duration}
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          className="t-fx-more"
          aria-expanded={moreOpen}
          aria-controls="exam-more"
          onClick={() => setMoreOpen((v) => !v)}
        >
          <Icon name={moreOpen ? "up" : "down"} size={16} />
          More settings
          <span className="t-muted t-small">
            {[
              draft.shuffle ? "shuffled" : null,
              draft.external.mode === "external" ? "on another website" : isPaper ? "on paper" : null,
              draft.date ? "scheduled" : null,
              draft.room.trim() ? draft.room.trim() : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </button>
        {moreOpen && (
          <div id="exam-more" className="t-fx-more-body">
            <div className="t-setting">
              <span className="t-setting-text">
                <label className="t-setting-title" htmlFor="exam-shuffle">
                  Shuffle questions per student
                </label>
                <span className="t-setting-sub" id="exam-shuffle-sub">
                  Neighbours see different orders
                </span>
              </span>
              <Switch id="exam-shuffle" checked={draft.shuffle} onChange={(v) => set({ shuffle: v })} describedBy="exam-shuffle-sub" />
            </div>
            <div className="t-grid-2">
              <div className="t-field">
                <label htmlFor="exam-course">Course code (optional)</label>
                <input
                  id="exam-course"
                  className="input"
                  value={draft.course}
                  maxLength={64}
                  placeholder="e.g. MATH 201"
                  onChange={(e) => set({ course: e.target.value })}
                  aria-invalid={!!err.course}
                />
                {err.course && <span className="field-error">{err.course}</span>}
              </div>
              <div className="t-field">
                <label htmlFor="exam-room">Room (optional)</label>
                <input
                  id="exam-room"
                  className="input"
                  value={draft.room}
                  maxLength={64}
                  placeholder="e.g. AB 1087"
                  onChange={(e) => set({ room: e.target.value })}
                  aria-invalid={!!err.room}
                />
                {err.room && <span className="field-error">{err.room}</span>}
              </div>
              <div className="t-field">
                <label htmlFor="exam-date">Date (for planning)</label>
                <input
                  id="exam-date"
                  type="date"
                  className="input"
                  value={draft.date}
                  onChange={(e) => set({ date: e.target.value })}
                  aria-invalid={!!err.schedule}
                />
              </div>
              <div className="t-field">
                <label htmlFor="exam-time">Start time</label>
                <input id="exam-time" type="time" className="input" value={draft.time} onChange={(e) => set({ time: e.target.value })} />
              </div>
            </div>
            {err.schedule && <span className="field-error">{err.schedule}</span>}
            <p className="t-small t-muted" style={{ margin: 0 }}>
              Students can start only after you press <strong>Open exam</strong> and read the start code aloud. Full-screen
              lockdown is always on.
            </p>
            {features.has("lms") && (
              <LmsCourseSelect
                value={lmsLink}
                onChange={(id, link) => {
                  setLmsLink(id);
                  if (link && !draft.course.trim()) set({ course: (link.code || link.name).slice(0, 64) });
                }}
              />
            )}
            <ExternalExamSettings
              value={draft.external}
              onChange={(external) => set({ external })}
              errors={err.external}
              delivery={draft.delivery}
              onDeliveryChange={(delivery) => set({ delivery })}
              allowPaper={features.has("scan")}
            />
            {!isPaper && appOptions.length > 0 && (
              <fieldset className="t-field t-apps">
                <legend>Apps students may use</legend>
                <p className="t-small t-muted" style={{ margin: "0 0 6px" }}>
                  Every other app is closed when the computer locks. VPN apps (like AnyConnect) always stay on.
                </p>
                <div className="t-apps-grid">
                  {appOptions.map((o) => {
                    const on = draft.allowedApps.includes(o.key);
                    return (
                      <label key={o.key} className="t-check">
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={() =>
                            set({
                              allowedApps: on
                                ? draft.allowedApps.filter((k) => k !== o.key)
                                : [...draft.allowedApps, o.key],
                            })
                          }
                        />
                        <span>
                          {o.name}
                          {o.risky && <span className="t-small t-warn-text"> · can open files / the web</span>}
                          {!o.mac && <span className="t-small t-muted"> · Windows only</span>}
                          {!o.windows && <span className="t-small t-muted"> · Mac only</span>}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>
            )}
            {draft.external.mode === "external" && (
              <div className="t-field" style={{ maxWidth: 260 }}>
                <label htmlFor="exam-external-points">Total points</label>
                <input
                  id="exam-external-points"
                  className="input"
                  inputMode="decimal"
                  value={draft.externalPoints}
                  onChange={(e) => set({ externalPoints: e.target.value })}
                  aria-invalid={!!err.externalPoints}
                  aria-describedby="exam-external-points-hint"
                />
                <span id="exam-external-points-hint" className="t-small t-muted">
                  After students finish, you enter each score in Results.
                </span>
                {err.externalPoints && <span className="field-error">{err.externalPoints}</span>}
              </div>
            )}
          </div>
        )}
      </section>

      {draft.external.mode !== "external" && (
        <section aria-labelledby="q-h" className="t-fx-questions">
          <h2 id="q-h" className="t-fx-qhead">
            Questions{" "}
            <span className="t-muted" style={{ fontWeight: 500 }}>
              · {draft.questions.length} · {totalPoints} pts
            </span>
          </h2>

          {draft.questions.length === 0 && (
            <div className="t-card">
              <div className="t-empty">
                <strong>No questions yet</strong>
                Add your first question below, or copy questions from an exam you wrote before.
              </div>
            </div>
          )}

          {draft.questions.map((q, i) => (
            <QuestionCard
              key={q.key}
              q={q}
              index={i}
              count={draft.questions.length}
              open={!!open[q.key]}
              onToggle={() => setOpen((o) => ({ ...o, [q.key]: !o[q.key] }))}
              errors={err.byKey[q.key]}
              onChange={(patch) => updateQ(q.key, patch)}
              onKind={(kind) => retypeQ(q.key, kind)}
              onMove={(delta) => moveQ(i, delta)}
              onRemove={() => removeQ(q.key)}
              topicSuggestions={topicSuggestions}
            />
          ))}

          <div className="t-card t-fx-add">
            <div className="t-addbar" role="group" aria-label="Add question">
              <span className="t-addbar-label">Add:</span>
              {QUICK.map((b) => (
                <button key={b.kind} type="button" className="t-chip-btn" onClick={() => addQ(b.kind)}>
                  {b.label}
                </button>
              ))}
              <AddQuestionMenu onAdd={addQ} />
            </div>
            <button type="button" className="t-link-btn" onClick={() => setCopying((v) => !v)} aria-expanded={copying}>
              <Icon name="copy" size={16} /> Copy questions from another exam
            </button>
            {copying && (
              <CopyFromExam
                exclude={exam?.id ?? created?.id}
                onCopy={(src) => {
                  const qs = copyQuestions(src);
                  setDraft((d) => ({ ...d, questions: [...d.questions, ...qs] }));
                  setCopying(false);
                }}
              />
            )}
          </div>
          {err.questions && <span className="field-error">{err.questions}</span>}
        </section>
      )}
    </form>
  );
}

/** Opening needs a class: ask for it right there (or make a new one). */
function OpenForClass({ onPick, onCancel }: { onPick: (classId: string) => void; onCancel: () => void }) {
  const [classId, setClassId] = useState("");
  return (
    <section className="t-card t-fx-ask" role="dialog" aria-labelledby="ask-class-h" aria-modal="false">
      <h2 id="ask-class-h">Which class takes this exam?</h2>
      <p className="t-muted" style={{ margin: 0 }}>
        Only students in that class can see and start it.
      </p>
      <ClassSelect value={classId} onChange={setClassId} idPrefix="ask" />
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="t-btn-outline" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" disabled={!classId} onClick={() => onPick(classId)}>
          Open exam
        </button>
      </div>
    </section>
  );
}

/** Pick one of your exams and add copies of all its questions. */
function CopyFromExam({ exclude, onCopy }: { exclude?: string; onCopy: (exam: Exam) => void }) {
  const [exams, setExams] = useState<Exam[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    teacherApi.listExams().then(
      (all) => alive && setExams(all.filter((e) => e.id !== exclude && e.mode !== "external" && e.question_count > 0)),
      () => alive && setError("Cannot load your exams."),
    );
    return () => {
      alive = false;
    };
  }, [exclude]);

  async function pick(id: string) {
    setBusy(id);
    try {
      onCopy(await teacherApi.getExam(id));
    } catch {
      setError("Cannot load that exam. Try again.");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <span className="field-error">{error}</span>;
  if (exams === null) return <span className="t-small t-muted">Loading your exams…</span>;
  if (exams.length === 0) return <span className="t-small t-muted">No other exams with questions yet.</span>;
  return (
    <ul className="t-cx-list" aria-label="Your exams">
      {exams.map((e) => (
        <li key={e.id} className="t-cx-row">
          <div style={{ minWidth: 0 }}>
            <strong>{e.title || "Untitled exam"}</strong>
            <div className="t-small t-muted">
              {e.question_count} question{e.question_count === 1 ? "" : "s"} · {e.total_points} pts
            </div>
          </div>
          <button type="button" className="t-btn-outline" disabled={busy !== null} onClick={() => void pick(e.id)}>
            {busy === e.id ? "Copying…" : "Copy its questions"}
          </button>
        </li>
      ))}
    </ul>
  );
}

function AddQuestionMenu({ onAdd }: { onAdd: (kind: DraftKind) => void }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const first = wrapRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]');
    first?.focus();
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  function onMenuKey(e: KeyboardEvent<HTMLDivElement>) {
    const items = Array.from(wrapRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    const i = items.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      btnRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      items[(i + 1) % items.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      items[(i - 1 + items.length) % items.length]?.focus();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  return (
    <div className="t-menu-wrap" ref={wrapRef}>
      <button
        ref={btnRef}
        type="button"
        className="t-add-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="add-q-menu"
        onClick={() => setOpen((o) => !o)}
      >
        + Add question
      </button>
      {open && (
        <div className="t-menu" role="menu" id="add-q-menu" aria-label="Question type" onKeyDown={onMenuKey}>
          {MENU.map((m) => (
            <button
              key={m.kind}
              type="button"
              role="menuitem"
              onClick={() => {
                onAdd(m.kind);
                setOpen(false);
              }}
            >
              {KIND_LABEL[m.kind]}
              <span>{m.hint}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function QuestionCard({
  q,
  index,
  count,
  open,
  onToggle,
  errors,
  onChange,
  onKind,
  onMove,
  onRemove,
  topicSuggestions = [],
}: {
  q: DraftQuestion;
  index: number;
  count: number;
  open: boolean;
  onToggle: () => void;
  errors?: QuestionErrors;
  onChange: (patch: Partial<DraftQuestion>) => void;
  onKind: (kind: DraftKind) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
  topicSuggestions?: TopicCount[];
}) {
  const id = (s: string) => `q${q.key}-${s}`;
  const n = index + 1;
  const e = errors ?? {};
  const bodyId = id("body");

  return (
    <section className={`t-q${errors ? " invalid" : ""}`} aria-label={`Question ${n}`}>
      <div className="t-q-summary">
        <span className="t-q-num" aria-hidden="true">
          {String(n).padStart(2, "0")}
        </span>
        <button type="button" className="t-q-toggle" aria-expanded={open} aria-controls={bodyId} onClick={onToggle}>
          <span className={`t-q-prompt${q.prompt.trim() ? "" : " placeholder"}`}>
            {q.prompt.trim() || "New question — click to write it"}
          </span>
          <span className="t-q-meta">
            {errors ? "Needs attention · " : ""}
            {questionMeta(q)}
          </span>
        </button>
        <div className="t-q-tools">
          <button type="button" className="t-icon-btn" onClick={() => onMove(-1)} disabled={index === 0} aria-label={`Move question ${n} up`}>
            <Icon name="up" size={18} />
          </button>
          <button
            type="button"
            className="t-icon-btn"
            onClick={() => onMove(1)}
            disabled={index === count - 1}
            aria-label={`Move question ${n} down`}
          >
            <Icon name="down" size={18} />
          </button>
          <button type="button" className="t-icon-btn danger" onClick={onRemove} aria-label={`Delete question ${n}`}>
            <Icon name="trash" size={18} />
          </button>
        </div>
      </div>

      {open && (
        <div className="t-q-body" id={bodyId}>
          <div className="t-field" style={{ maxWidth: 260 }}>
            <label htmlFor={id("kind")}>Question type</label>
            <select
              id={id("kind")}
              className="t-select"
              value={q.tf ? "tf" : q.type}
              onChange={(ev) => onKind(ev.target.value as DraftKind)}
            >
              {MENU.map((m) => (
                <option key={m.kind} value={m.kind}>
                  {KIND_LABEL[m.kind]}
                </option>
              ))}
            </select>
          </div>
          <div className="t-grid-prompt">
            <div className="t-field">
              <label htmlFor={id("prompt")}>Question text</label>
              <textarea
                id={id("prompt")}
                className="textarea"
                rows={q.type === "essay" ? 3 : 2}
                value={q.prompt}
                onChange={(ev) => onChange({ prompt: ev.target.value })}
                aria-invalid={!!e.prompt}
              />
              {e.prompt && <span className="field-error">{e.prompt}</span>}
            </div>
            <div className="t-field">
              <label htmlFor={id("points")}>Points</label>
              <input
                id={id("points")}
                className="input mono"
                inputMode="numeric"
                value={q.points}
                onChange={(ev) => onChange({ points: ev.target.value })}
                aria-invalid={!!e.points}
              />
              {e.points && <span className="field-error">{e.points}</span>}
            </div>
          </div>

          <QuestionImagesEditor idPrefix={id("images")} images={q.images ?? []} onChange={(images) => onChange({ images })} />

          <div className="t-field">
            <label htmlFor={id("topics")}>Topics (optional)</label>
            <TopicInput
              id={id("topics")}
              value={q.topics ?? []}
              onChange={(topics) => onChange({ topics })}
              suggestions={topicSuggestions}
              invalid={!!e.topics}
              describedBy={id("topics-hint")}
            />
            {e.topics ? (
              <span className="field-error" id={id("topics-hint")}>
                {e.topics}
              </span>
            ) : (
              <span className="hint" id={id("topics-hint")}>
                Press Enter or comma after each topic (up to 5). Helps you find weak spots later.
              </span>
            )}
          </div>

          {q.type === "mcq" && q.tf && <TrueFalseEditor q={q} id={id} error={e.correct} onChange={onChange} />}
          {q.type === "mcq" && !q.tf && <OptionsEditor q={q} id={id} errors={e} onChange={onChange} />}

          {q.type === "numeric" && (
            <div className="t-grid-2">
              <div className="t-field">
                <label htmlFor={id("answer")}>Correct answer</label>
                <input
                  id={id("answer")}
                  className="input mono"
                  inputMode="decimal"
                  value={q.answer_number}
                  onChange={(ev) => onChange({ answer_number: ev.target.value })}
                  aria-invalid={!!e.answer}
                />
                {e.answer && <span className="field-error">{e.answer}</span>}
              </div>
              <div className="t-field">
                <label htmlFor={id("tolerance")}>Tolerance (±)</label>
                <input
                  id={id("tolerance")}
                  className="input mono"
                  inputMode="decimal"
                  value={q.tolerance}
                  onChange={(ev) => onChange({ tolerance: ev.target.value })}
                  aria-invalid={!!e.tolerance}
                />
                {e.tolerance ? (
                  <span className="field-error">{e.tolerance}</span>
                ) : (
                  <span className="hint">0 means the answer must match exactly.</span>
                )}
              </div>
            </div>
          )}

          {q.type === "short" && (
            <div className="t-field">
              <label htmlFor={id("accepted")}>Accepted answers (comma separated)</label>
              <input
                id={id("accepted")}
                className="input"
                value={q.accepted}
                placeholder="e.g. Paris, paris france"
                onChange={(ev) => onChange({ accepted: ev.target.value })}
                aria-invalid={!!e.accepted}
              />
              {e.accepted ? (
                <span className="field-error">{e.accepted}</span>
              ) : (
                <span className="hint">Case and extra spaces are ignored. Other answers go to your grading queue.</span>
              )}
            </div>
          )}

          {q.type === "essay" && <EssayEditor q={q} id={id} errors={e} onChange={onChange} />}
        </div>
      )}
    </section>
  );
}

function OptionsEditor({
  q,
  id,
  errors: e,
  onChange,
}: {
  q: DraftQuestion;
  id: (s: string) => string;
  errors: QuestionErrors;
  onChange: (patch: Partial<DraftQuestion>) => void;
}) {
  const setOption = (i: number, value: string) => onChange({ options: q.options.map((o, j) => (j === i ? value : o)) });
  const addOption = () => q.options.length < 8 && onChange({ options: [...q.options, ""] });
  const removeOption = (i: number) => {
    if (q.options.length <= 2) return;
    let correct = q.correct_index;
    if (correct === i) correct = null;
    else if (correct != null && correct > i) correct -= 1;
    onChange({ options: q.options.filter((_, j) => j !== i), correct_index: correct });
  };
  return (
    <fieldset style={{ border: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
      <legend className="t-label" style={{ marginBottom: 8, padding: 0 }}>
        Options — select the correct one
      </legend>
      {q.options.map((opt, i) => (
        <div className="t-opt-row" key={i}>
          <input
            type="radio"
            name={id("correct")}
            id={id(`correct-${i}`)}
            checked={q.correct_index === i}
            onChange={() => onChange({ correct_index: i })}
            aria-label={`Option ${LETTERS[i]} is correct`}
          />
          <label className="t-opt-letter" htmlFor={id(`opt-${i}`)}>
            {LETTERS[i]}
          </label>
          <input
            id={id(`opt-${i}`)}
            className="input"
            value={opt}
            placeholder={`Option ${LETTERS[i]}`}
            onChange={(ev) => setOption(i, ev.target.value)}
            aria-invalid={!!e.options && !opt.trim()}
          />
          {q.correct_index === i && <span className="t-pill green">Correct</span>}
          <button
            type="button"
            className="t-icon-btn"
            onClick={() => removeOption(i)}
            disabled={q.options.length <= 2}
            aria-label={`Remove option ${LETTERS[i]}`}
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      ))}
      <button type="button" className="t-link-btn" onClick={addOption} disabled={q.options.length >= 8}>
        + Add option
      </button>
      {e.options && <span className="field-error">{e.options}</span>}
      {e.correct && <span className="field-error">{e.correct}</span>}
    </fieldset>
  );
}

function TrueFalseEditor({
  q,
  id,
  error,
  onChange,
}: {
  q: DraftQuestion;
  id: (s: string) => string;
  error?: string;
  onChange: (patch: Partial<DraftQuestion>) => void;
}) {
  return (
    <fieldset style={{ border: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
      <legend className="t-label" style={{ marginBottom: 8, padding: 0 }}>
        Correct answer
      </legend>
      <div className="t-tf">
        {["True", "False"].map((label, i) => (
          <label key={label} htmlFor={id(`tf-${i}`)}>
            <input
              type="radio"
              id={id(`tf-${i}`)}
              name={id("tf")}
              checked={q.correct_index === i}
              onChange={() => onChange({ correct_index: i })}
            />
            {label}
          </label>
        ))}
      </div>
      {error && <span className="field-error">{error}</span>}
    </fieldset>
  );
}

function EssayEditor({
  q,
  id,
  errors: e,
  onChange,
}: {
  q: DraftQuestion;
  id: (s: string) => string;
  errors: QuestionErrors;
  onChange: (patch: Partial<DraftQuestion>) => void;
}) {
  const setRow = (key: string, patch: Partial<DraftRubricRow>) =>
    onChange({ rubric: q.rubric.map((r) => (r.key === key ? { ...r, ...patch } : r)) });
  const sum = rubricSum(q.rubric);
  const target = Number(q.points) || 0;
  return (
    <>
      <fieldset style={{ border: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <legend className="t-label" style={{ marginBottom: 8, padding: 0 }}>
          Rubric (optional) — criteria you grade on
        </legend>
        {q.rubric.length === 0 && (
          <span className="hint">Without a rubric you give one overall score when grading.</span>
        )}
        {q.rubric.map((r, i) => (
          <div className="t-rubric-row" key={r.key}>
            <input
              id={id(`rubric-${i}-label`)}
              className="input"
              value={r.label}
              placeholder={`Criterion ${i + 1}, e.g. "Two clear reasons"`}
              aria-label={`Criterion ${i + 1} description`}
              onChange={(ev) => setRow(r.key, { label: ev.target.value })}
              aria-invalid={!!e.rubric && !r.label.trim()}
            />
            <input
              id={id(`rubric-${i}-points`)}
              className="input mono"
              inputMode="numeric"
              value={r.points}
              placeholder="pts"
              aria-label={`Criterion ${i + 1} points`}
              onChange={(ev) => setRow(r.key, { points: ev.target.value })}
            />
            <button
              type="button"
              className="t-icon-btn"
              aria-label={`Remove criterion ${i + 1}`}
              onClick={() => onChange({ rubric: q.rubric.filter((x) => x.key !== r.key) })}
            >
              <Icon name="close" size={16} />
            </button>
          </div>
        ))}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, justifyContent: "space-between" }}>
          <button type="button" className="t-link-btn" onClick={() => onChange({ rubric: [...q.rubric, newRubricRow()] })}>
            + Add criterion
          </button>
          {q.rubric.length > 0 && (
            <span className={`t-rubric-total ${sum === target ? "ok" : "bad"}`} aria-live="polite">
              Rubric total {sum} / {target} pts
            </span>
          )}
        </div>
        {e.rubric && <span className="field-error">{e.rubric}</span>}
      </fieldset>
      <div className="t-grid-2">
        <div className="t-field">
          <label htmlFor={id("min-words")}>Minimum words</label>
          <input
            id={id("min-words")}
            className="input mono"
            inputMode="numeric"
            value={q.min_words}
            placeholder="No minimum"
            onChange={(ev) => onChange({ min_words: ev.target.value })}
            aria-invalid={!!e.words}
          />
        </div>
        <div className="t-field">
          <label htmlFor={id("max-words")}>Maximum words</label>
          <input
            id={id("max-words")}
            className="input mono"
            inputMode="numeric"
            value={q.max_words}
            placeholder="No maximum"
            onChange={(ev) => onChange({ max_words: ev.target.value })}
            aria-invalid={!!e.words}
          />
        </div>
      </div>
      {e.words ? (
        <span className="field-error">{e.words}</span>
      ) : (
        <span className="hint">Essays are never auto-graded. Students see the rubric and word limits.</span>
      )}
    </>
  );
}


/** Audit S-03: pick the class that takes the exam (optional until it opens), or make one. */
function ClassSelect({
  value,
  onChange,
  idPrefix = "exam",
}: {
  value: string;
  onChange: (id: string) => void;
  idPrefix?: string;
}) {
  const [classes, setClasses] = useState<SchoolClass[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const fieldId = `${idPrefix}-class`;

  useEffect(() => {
    let alive = true;
    classesApi.list().then(
      (k) => alive && setClasses(k),
      () => alive && setClasses([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    setProblem(null);
    try {
      const k = await classesApi.create(name.trim());
      setClasses((c) => [...(c ?? []), k]);
      onChange(String(k.id));
      setAdding(false);
      setName("");
    } catch (e) {
      setProblem(e instanceof ApiError ? e.detail : "Cannot reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="t-field" style={{ flex: "1 1 240px", minWidth: 0 }}>
      <label htmlFor={fieldId}>Class</label>
      {!adding ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <select
            id={fieldId}
            className="t-select"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            aria-describedby={`${fieldId}-help`}
            disabled={classes === null}
            style={{ flex: "1 1 180px", minWidth: 0 }}
          >
            <option value="">{classes === null ? "Loading classes…" : "No class yet (save for later)"}</option>
            {(classes ?? []).map((k) => (
              <option key={k.id} value={String(k.id)}>
                {k.name}
                {k.term ? ` · ${k.term}` : ""} · {k.member_count} student{k.member_count === 1 ? "" : "s"}
              </option>
            ))}
          </select>
          <button type="button" className="t-mini-link" onClick={() => setAdding(true)}>
            + New class
          </button>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input
            id={fieldId}
            className="input"
            placeholder="Class name, e.g. CS 101 — A"
            value={name}
            maxLength={120}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void create();
              }
            }}
            style={{ flex: "1 1 180px", minWidth: 0 }}
          />
          <button type="button" className="btn btn-secondary" disabled={busy || !name.trim()} onClick={() => void create()}>
            {busy ? "Creating…" : "Create"}
          </button>
          <button type="button" className="t-mini-link" onClick={() => setAdding(false)}>
            cancel
          </button>
        </div>
      )}
      <span id={`${fieldId}-help`} className="t-small t-muted">
        Only students in this class see the exam. You can choose it later, when you open the exam.
      </span>
      {problem && <span className="field-error">{problem}</span>}
    </div>
  );
}
