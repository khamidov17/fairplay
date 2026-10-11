// /teacher/homework/new[?class=<id>]: create a paper assignment for one or more classes.
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { classesApi, type ClassSummary } from "../../../api/classes";
import { homeworkApi } from "../../../api/homework";
import { Loading } from "../../../components/Loading";
import { errorText } from "../../../components/teacher/util";
import { Switch } from "../../../components/ui/Switch";
import "../../../styles/homework.css";
import { newQuestion, type DraftQuestion, type QuestionErrors } from "../examDraft";
import { defaultDue, dueIso, validateQuestions } from "./homeworkLogic";
import { QuestionsEditor } from "./QuestionsEditor";

export default function HomeworkNewPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const pre = Number(params.get("class") || 0);
  const [classes, setClasses] = useState<ClassSummary[] | null>(null);
  const [classIds, setClassIds] = useState<number[]>(pre ? [pre] : []);
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const due0 = defaultDue();
  const [date, setDate] = useState(due0.date);
  const [time, setTime] = useState(due0.time);
  const [allowLate, setAllowLate] = useState(true);
  const [allowResubmit, setAllowResubmit] = useState(true);
  const [autoRelease, setAutoRelease] = useState(false);
  const [questions, setQuestions] = useState<DraftQuestion[]>(() => [newQuestion("essay")]);
  const [qErrors, setQErrors] = useState<Record<string, QuestionErrors>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    classesApi.list().then(setClasses, (e) => setError(errorText(e)));
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const v = validateQuestions(questions);
    setQErrors(v.errors.byKey);
    const due = dueIso(date, time);
    if (!title.trim()) return setError("Give the homework a title.");
    if (classIds.length === 0) return setError("Pick at least one class.");
    if (!due) return setError("Pick a due date.");
    if (!v.ok) return setError(v.errors.questions || "Fix the questions marked in red.");
    setBusy(true);
    setError(null);
    try {
      const hw = await homeworkApi.create({
        title: title.trim(),
        due_at: due,
        class_ids: classIds,
        instructions,
        allow_late: allowLate,
        allow_resubmit: allowResubmit,
        auto_release: autoRelease,
        questions: v.inputs,
      });
      navigate(`/teacher/homework/${hw.exam_id}?tab=key`);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="hw-new" aria-labelledby="hw-new-h">
      <Link to="/teacher/homework" className="t-back">
        ← Homework
      </Link>
      <header className="t-page-head">
        <div className="t-head-text">
          <h1 id="hw-new-h">New paper assignment</h1>
          <p>Students do it on paper and hand it in with their phone camera. The AI grades every step; you review and release.</p>
        </div>
      </header>

      <section className="t-card" aria-labelledby="hw-basics-h">
        <h2 id="hw-basics-h">Assignment</h2>
        <div className="t-field">
          <label htmlFor="hw-title">Title</label>
          <input id="hw-title" className="input" value={title} maxLength={200} placeholder="e.g. Week 3 — factoring" onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="t-grid-2">
          <div className="t-field">
            <label htmlFor="hw-date">Due date</label>
            <input id="hw-date" type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
          </div>
          <div className="t-field">
            <label htmlFor="hw-time">Due time</label>
            <input id="hw-time" type="time" className="input" value={time} onChange={(e) => setTime(e.target.value)} />
          </div>
        </div>
        <fieldset className="t-field">
          <legend className="t-label">Classes</legend>
          {!classes ? (
            <Loading label="Loading classes…" />
          ) : classes.length === 0 ? (
            <p className="t-muted">
              You have no classes yet. <Link to="/teacher/classes">Create a class</Link> first.
            </p>
          ) : (
            <div className="hw-class-picks">
              {classes.map((c) => (
                <label key={c.id} className="hw-check">
                  <input
                    type="checkbox"
                    checked={classIds.includes(c.id)}
                    onChange={(e) => setClassIds(e.target.checked ? [...classIds, c.id] : classIds.filter((x) => x !== c.id))}
                  />
                  {c.name}
                  <span className="t-muted t-small">{c.member_count} students</span>
                </label>
              ))}
            </div>
          )}
        </fieldset>
        <div className="t-field">
          <label htmlFor="hw-instr">Instructions for students (optional)</label>
          <textarea id="hw-instr" className="textarea" rows={2} value={instructions} maxLength={5000} placeholder="Write on lined paper, one question per page if you can." onChange={(e) => setInstructions(e.target.value)} />
        </div>
      </section>

      <section className="t-card" aria-labelledby="hw-q-h">
        <h2 id="hw-q-h">Questions</h2>
        <QuestionsEditor questions={questions} onChange={setQuestions} errors={qErrors} />
      </section>

      <section className="t-card" aria-labelledby="hw-set-h">
        <h2 id="hw-set-h">Hand-in and feedback</h2>
        <div className="t-setting">
          <div className="t-setting-text">
            <span className="t-setting-title">Accept late work</span>
            <span className="t-setting-sub">After the due date students can still hand in; it is marked late.</span>
          </div>
          <Switch checked={allowLate} onChange={setAllowLate} label="Accept late work" />
        </div>
        <div className="t-setting">
          <div className="t-setting-text">
            <span className="t-setting-title">Students may hand in again until the due date</span>
            <span className="t-setting-sub">The newest photos replace the earlier ones.</span>
          </div>
          <Switch checked={allowResubmit} onChange={setAllowResubmit} label="Allow handing in again" />
        </div>
        <div className="t-setting">
          <div className="t-setting-text">
            <span className="t-setting-title">Release feedback automatically</span>
            <span className="t-setting-sub">As soon as you have reviewed every answer (after the due date, or when everyone handed in).</span>
          </div>
          <Switch checked={autoRelease} onChange={setAutoRelease} label="Release automatically" />
        </div>
      </section>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      <div className="hw-actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Creating…" : "Create homework"}
        </button>
        <Link to="/teacher/homework" className="btn btn-secondary">
          Cancel
        </Link>
      </div>
    </form>
  );
}
