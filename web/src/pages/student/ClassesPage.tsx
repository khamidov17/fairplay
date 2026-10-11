// /student/classes: the student's classes with their exams, and "Join a class" by code.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../../api/client";
import { classesApi, classLabel, type StudentClass, type StudentClassExam } from "../../api/classes";
import { shortDate, shortTime } from "../../components/student/format";
import { StudentLayout, StuLoading } from "../../components/student/StudentLayout";
import "../../styles/classes.css";

export default function ClassesPage() {
  const [classes, setClasses] = useState<StudentClass[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setClasses(await classesApi.mine());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Cannot reach the server. Try again in a moment.");
      setClasses((c) => c ?? []);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <StudentLayout>
      <main className="stu-main" style={{ gap: 24 }}>
        <h1 className="stu-h1">Classes</h1>
        <JoinClassForm onJoined={reload} />
        {error && (
          <div className="stu-alert warn" role="alert">
            {error}
          </div>
        )}
        {classes === null ? (
          <StuLoading label="Loading your classes…" />
        ) : classes.length === 0 ? (
          <section className="stu-card">
            <h2>No classes yet</h2>
            <p className="stu-muted">Ask your teacher for the class code and type it above. Your teacher can also add you.</p>
          </section>
        ) : (
          classes.map((c) => <ClassCard key={c.id} cls={c} />)
        )}
      </main>
    </StudentLayout>
  );
}

export function JoinClassForm({ onJoined }: { onJoined: () => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joined, setJoined] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const clean = code.replace(/[\s-]/g, "").toUpperCase();
    setJoined(null);
    if (!clean) {
      setError("Type the class code from your teacher.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await classesApi.join(clean);
      setJoined(r.already_member ? `You are already in ${r.name}.` : `You joined ${r.name}${r.teacher_name ? ` (${r.teacher_name})` : ""}.`);
      setCode("");
      onJoined();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Cannot reach the server. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stu-card" aria-labelledby="join-h">
      <h2 id="join-h">Join a class</h2>
      <form className="stu-join" onSubmit={submit} noValidate>
        <div className="stu-field">
          <label htmlFor="join-code">Class code</label>
          <input
            id="join-code"
            value={code}
            maxLength={16}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder="ABC2345"
            onChange={(e) => setCode(e.target.value)}
            aria-invalid={!!error}
            aria-describedby={error ? "join-err" : undefined}
          />
        </div>
        <button type="submit" className="stu-btn primary" disabled={busy}>
          {busy ? "Joining…" : "Join"}
        </button>
      </form>
      {error && (
        <div id="join-err" className="stu-alert error" role="alert">
          {error}
        </div>
      )}
      {joined && (
        <div className="stu-alert success" role="status">
          {joined}
        </div>
      )}
    </section>
  );
}

function examState(e: StudentClassExam): { text: string; action?: { to: string; label: string } } {
  if (e.attempt?.status === "submitted") return { text: "Submitted" };
  if (e.attempt?.status === "in_progress")
    return { text: "In progress", action: { to: `/student/exam/${e.attempt.attempt_id}`, label: "Continue" } };
  if (e.status === "live") return { text: "Open now", action: { to: `/student/exams/${e.id}/checkin`, label: "Check in" } };
  if (e.status === "closed") return { text: "Closed" };
  return { text: e.scheduled_at ? `Planned ${shortDate(e.scheduled_at)} ${shortTime(e.scheduled_at)}` : "Planned" };
}

function ClassCard({ cls }: { cls: StudentClass }) {
  const sub = [cls.section, cls.term, cls.teacher_name].filter(Boolean).join(" · ");
  return (
    <section className="stu-card stu-class-card" aria-labelledby={`class-${cls.id}`}>
      <div className="stu-class-head">
        <h2 id={`class-${cls.id}`}>{cls.name}</h2>
        {sub && <span className="stu-muted">{sub}</span>}
      </div>
      {cls.exams.length === 0 ? (
        <p className="stu-muted" style={{ margin: 0 }}>
          No exams in this class yet.
        </p>
      ) : (
        <div className="stu-class-exams" aria-label={`Exams in ${classLabel(cls)}`}>
          {cls.exams.map((e) => {
            const st = examState(e);
            return (
              <div key={e.id} className="stu-class-exam">
                <span>{e.course ? `${e.course} · ${e.title}` : e.title}</span>
                <span className="stu-class-exam-when">{st.text}</span>
                {st.action && (
                  <Link to={st.action.to} className="stu-btn primary sm">
                    {st.action.label}
                  </Link>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
