// /teacher/homework/:id — one paper assignment: who handed in, the answer key, scanning the
// class stack, the shared review screen (grade by question) and releasing feedback.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { homeworkApi, type HomeworkDetail } from "../../../api/homework";
import { Loading } from "../../../components/Loading";
import { Pill } from "../../../components/teacher/TeacherLayout";
import { errorText, formatWhen, plural } from "../../../components/teacher/util";
import { Switch } from "../../../components/ui/Switch";
import "../../../styles/homework.css";
import "../../../styles/scan.css";
import { AnswerKeyStep } from "../scan/AnswerKeyStep";
import { PrintStep } from "../scan/PrintStep";
import { QueueStep } from "../scan/QueueStep";
import { UploadStep } from "../scan/UploadStep";
import { STATUS_TONE, dueIso, nextStep, rowStatusText } from "./homeworkLogic";

type Tab = "students" | "key" | "collect" | "review" | "settings";
const TABS: { id: Tab; label: string }[] = [
  { id: "students", label: "Students" },
  { id: "key", label: "Answer key" },
  { id: "collect", label: "Scan class stack" },
  { id: "review", label: "Review" },
  { id: "settings", label: "Settings" },
];

export default function HomeworkPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab") as Tab | null;
  const tab: Tab = TABS.some((t) => t.id === raw) ? (raw as Tab) : "students";
  const [hw, setHw] = useState<HomeworkDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await homeworkApi.get(id);
      setHw(d);
      setError(null);
      if (timer.current) clearTimeout(timer.current);
      if (d.students.some((s) => s.processing === "processing" || s.processing === "grading"))
        timer.current = setTimeout(load, 3000);
    } catch (e) {
      setError(errorText(e));
    }
  }, [id]);

  useEffect(() => {
    load();
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  async function release(force = false) {
    setBusy(true);
    setNotice(null);
    try {
      setHw(await homeworkApi.release(id, force));
      setNotice("Feedback released. Students see it now.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function unrelease() {
    setBusy(true);
    try {
      setHw(await homeworkApi.unrelease(id));
      setNotice("Feedback hidden again. Auto-release is off.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (!hw) return error ? <div className="alert alert-error" role="alert">{error}</div> : <Loading label="Loading homework…" />;

  const c = hw.counts;
  return (
    <>
      <Link to="/teacher/homework" className="t-back">
        ← Homework
      </Link>
      <header className="t-page-head">
        <div className="t-head-text">
          <span className="t-eyebrow">Paper assignment · {hw.classes.map((x) => x.name).join(", ")}</span>
          <h1>{hw.title}</h1>
          <p>
            Due {formatWhen(hw.due_at)} · {plural(hw.question_count, "question")} · {hw.total_points} points
            {hw.past_due && " · past due"}
          </p>
        </div>
        <div className="t-head-actions">
          {hw.grades_released ? (
            <button type="button" className="btn btn-secondary" disabled={busy} onClick={unrelease}>
              Hide feedback
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || c.handed_in === 0 || hw.pending > 0}
              title={hw.pending > 0 ? `${hw.pending} answers still need your review` : undefined}
              onClick={() => release(false)}
            >
              Release feedback
            </button>
          )}
        </div>
      </header>

      <div className="t-stats hw-stats" aria-label="Hand-in progress">
        <Stat value={`${c.handed_in}/${c.total}`} label="Handed in" />
        <Stat value={c.submitted} label="Being graded" tone={c.submitted ? "orange" : "grey"} />
        <Stat value={c.ai_graded} label="AI graded — to review" tone={c.ai_graded ? "blue" : "grey"} />
        <Stat value={c.reviewed + c.released} label="Reviewed" tone="green" />
        <Stat value={c.late} label="Late" tone={c.late ? "orange" : "grey"} />
      </div>
      <p className="hw-next" aria-live="polite">
        {nextStep(c, hw.grades_released)}
        {!hw.grades_released && hw.auto_release && " Auto-release is on."}
        {!hw.ai_enabled && " AI is off on this server: drafts say “grade by hand”."}
      </p>
      {notice && (
        <div className="alert alert-success" role="status">
          {notice}
        </div>
      )}
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <div className="t-tabs" role="tablist" aria-label="Homework sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            className="t-tab"
            aria-selected={tab === t.id}
            onClick={() => setParams({ tab: t.id }, { replace: true })}
          >
            {t.label}
            {t.id === "review" && hw.pending > 0 ? ` (${hw.pending})` : ""}
          </button>
        ))}
      </div>

      {tab === "students" && <StudentsTab hw={hw} onReview={() => setParams({ tab: "review" }, { replace: true })} />}
      {tab === "key" && (
        <div className="sc-panel">
          <AnswerKeyStep examId={hw.exam_id} />
        </div>
      )}
      {tab === "collect" && (
        <div className="sc-panel hw-collect">
          <p className="t-muted">
            Students hand in from the student app (Homework → this assignment). To collect paper in class instead, scan
            or photograph the stack here — choose “not printed by FairPlay” for plain paper, or print booklets with codes first.
          </p>
          <UploadStep examId={hw.exam_id} onChanged={load} />
          <details className="hw-print">
            <summary>Print booklets with codes (optional)</summary>
            <PrintStep examId={hw.exam_id} title={hw.title} />
          </details>
        </div>
      )}
      {tab === "review" && (
        <div className="sc-panel">
          <QueueStep examId={hw.exam_id} onChanged={load} />
        </div>
      )}
      {tab === "settings" && <SettingsTab hw={hw} onSaved={setHw} />}
    </>
  );
}

function Stat({ value, label, tone = "grey" }: { value: number | string; label: string; tone?: string }) {
  return (
    <div className="t-stat">
      <span className={`t-stat-value ${tone}`}>{value}</span>
      <span className="t-stat-label">{label}</span>
    </div>
  );
}

function StudentsTab({ hw, onReview }: { hw: HomeworkDetail; onReview: () => void }) {
  if (hw.students.length === 0)
    return (
      <div className="t-card">
        <div className="t-empty">
          <strong>No students</strong>
          Add students to {hw.classes.map((c) => c.name).join(", ")} first.
        </div>
      </div>
    );
  return (
    <div className="t-card t-table-scroll">
      <table className="t-table hw-table">
        <thead>
          <tr>
            <th>Student</th>
            <th>Status</th>
            <th>Handed in</th>
            <th>Pages</th>
            <th className="num">Score</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {hw.students.map((s) => (
            <tr key={s.id}>
              <td>
                <Link className="t-row-link strong" to={`/teacher/progress/students/${encodeURIComponent(s.username)}`}>
                  {s.name}
                </Link>
                <div className="t-muted t-small">{s.username}</div>
              </td>
              <td>
                <Pill tone={s.processing === "failed" ? "red" : STATUS_TONE[s.status]}>{rowStatusText(s)}</Pill>
                {s.late && (
                  <Pill tone="orange">Late</Pill>
                )}
                {s.error && <div className="t-small t-muted">{s.error}</div>}
              </td>
              <td>
                {s.submitted_at ? formatWhen(s.submitted_at) : "—"}
                {s.source === "scan" && <div className="t-muted t-small">scanned by you</div>}
              </td>
              <td>
                <div className="hw-thumbs">
                  {s.pages.slice(0, 4).map((u, i) => (
                    <a key={u} href={u} target="_blank" rel="noreferrer" aria-label={`${s.name}, page ${i + 1}`}>
                      <img src={u} alt="" loading="lazy" />
                    </a>
                  ))}
                  {s.pages.length > 4 && <span className="t-muted t-small">+{s.pages.length - 4}</span>}
                </div>
              </td>
              <td className="num">{s.percent != null ? `${s.percent}%` : "—"}</td>
              <td>
                {s.needs_grading > 0 && (
                  <button type="button" className="t-link-btn" onClick={onReview}>
                    Review {s.needs_grading}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SettingsTab({ hw, onSaved }: { hw: HomeworkDetail; onSaved: (h: HomeworkDetail) => void }) {
  const navigate = useNavigate();
  const d = new Date(hw.due_at);
  const p2 = (n: number) => String(n).padStart(2, "0");
  const [date, setDate] = useState(`${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`);
  const [time, setTime] = useState(`${p2(d.getHours())}:${p2(d.getMinutes())}`);
  const [instructions, setInstructions] = useState(hw.instructions);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function patch(p: Parameters<typeof homeworkApi.update>[1], done = "Saved.") {
    setBusy(true);
    setErr(null);
    try {
      onSaved(await homeworkApi.update(hw.exam_id, p));
      setMsg(done);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function worksheet(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setErr(null);
    try {
      await homeworkApi.uploadWorksheet(hw.exam_id, file);
      onSaved(await homeworkApi.get(hw.exam_id));
      setMsg("Worksheet shared with the class.");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="t-card hw-settings" aria-label="Settings">
      <div className="t-grid-2">
        <div className="t-field">
          <label htmlFor="hws-date">Due date</label>
          <input id="hws-date" type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
        <div className="t-field">
          <label htmlFor="hws-time">Due time</label>
          <input id="hws-time" type="time" className="input" value={time} onChange={(e) => setTime(e.target.value)} />
        </div>
      </div>
      <div className="t-field">
        <label htmlFor="hws-instr">Instructions for students</label>
        <textarea id="hws-instr" className="textarea" rows={2} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
      </div>
      <button
        type="button"
        className="btn btn-secondary"
        disabled={busy}
        onClick={() => {
          const due = dueIso(date, time);
          if (!due) return setErr("Pick a due date.");
          patch({ due_at: due, instructions });
        }}
      >
        Save due date and instructions
      </button>

      <div className="t-setting">
        <div className="t-setting-text">
          <span className="t-setting-title">Accept late work</span>
        </div>
        <Switch checked={hw.allow_late} disabled={busy} onChange={(v) => patch({ allow_late: v })} label="Accept late work" />
      </div>
      <div className="t-setting">
        <div className="t-setting-text">
          <span className="t-setting-title">Students may hand in again until the due date</span>
        </div>
        <Switch checked={hw.allow_resubmit} disabled={busy} onChange={(v) => patch({ allow_resubmit: v })} label="Allow handing in again" />
      </div>
      <div className="t-setting">
        <div className="t-setting-text">
          <span className="t-setting-title">Release feedback automatically when everything is reviewed</span>
          <span className="t-setting-sub">After the due date, or as soon as every student handed in.</span>
        </div>
        <Switch checked={hw.auto_release} disabled={busy} onChange={(v) => patch({ auto_release: v })} label="Release automatically" />
      </div>

      <div className="t-field">
        <span className="t-label">Worksheet to print or share (PDF or picture)</span>
        {hw.worksheet ? (
          <div className="hw-row">
            <button type="button" className="t-link-btn" onClick={() => homeworkApi.downloadWorksheet(hw.exam_id, hw.worksheet!.name)}>
              {hw.worksheet.name}
            </button>
            <button
              type="button"
              className="t-link-btn"
              disabled={busy}
              onClick={async () => {
                await homeworkApi.removeWorksheet(hw.exam_id);
                onSaved(await homeworkApi.get(hw.exam_id));
              }}
            >
              Remove
            </button>
          </div>
        ) : (
          <input type="file" accept="application/pdf,image/png,image/jpeg" aria-label="Upload worksheet" onChange={(e) => worksheet(e.target.files?.[0])} />
        )}
      </div>

      {msg && <p className="t-muted" role="status">{msg}</p>}
      {err && (
        <div className="alert alert-error" role="alert">
          {err}
        </div>
      )}
      <hr />
      <button
        type="button"
        className="t-btn-warn"
        onClick={async () => {
          if (!window.confirm("Delete this homework and every student's work in it?")) return;
          await homeworkApi.remove(hw.exam_id);
          navigate("/teacher/homework");
        }}
      >
        Delete homework
      </button>
    </section>
  );
}
