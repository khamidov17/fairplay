// Audit 2026-10-12 S-03: a student joins a class with the code the teacher gives out.
// Exams of that class then appear on the home page.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, NetworkError, studentClassesApi } from "../../api/client";
import type { StudentClass } from "../../api/types";
import { takeJoinCode } from "../../pages/student/JoinLinkPage";

export function JoinClass({ onJoined }: { onJoined?: () => void }) {
  const [classes, setClasses] = useState<StudentClass[] | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setClasses(await studentClassesApi.list());
    } catch {
      setClasses((c) => c ?? []);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 2026-10-13: arrived through a class link -> join straight away.
  useEffect(() => {
    const pending = takeJoinCode();
    if (pending) void doJoin(pending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function join(e: FormEvent) {
    e.preventDefault();
    if (!code.trim()) return;
    await doJoin(code);
  }

  async function doJoin(value: string) {
    setBusy(true);
    setMessage(null);
    try {
      const k = await studentClassesApi.join(value);
      setMessage({ ok: true, text: `You joined ${k.name} (${k.teacher}). Its exams will show up here.` });
      setCode("");
      await load();
      onJoined?.();
    } catch (err) {
      setMessage({
        ok: false,
        text: err instanceof ApiError ? err.detail : err instanceof NetworkError ? "No connection. Try again." : "Could not join.",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stu-card" aria-labelledby="classes-h" style={{ gap: 10 }}>
      <h2 id="classes-h">Your classes</h2>
      {classes && classes.length > 0 ? (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {classes.map((k) => (
            <li key={k.id}>
              {k.name} <span className="stu-muted">· {[k.term, k.teacher].filter(Boolean).join(" · ")}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="stu-muted" style={{ margin: 0 }}>
          {classes === null ? "Loading…" : "You are not in a class yet. Exams only show up for your classes."}
        </p>
      )}
      <form onSubmit={join} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <label htmlFor="join-code" className="sr-only">
          Class code
        </label>
        <input
          id="join-code"
          className="stu-answer-input"
          placeholder="Class code, e.g. K7QM-3XPA"
          value={code}
          maxLength={20}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setCode(e.target.value.toUpperCase())}
          style={{ flex: "1 1 180px", minWidth: 0, letterSpacing: "0.08em" }}
        />
        <button type="submit" className="stu-btn primary" disabled={busy || code.trim().length < 8}>
          {busy ? "Joining…" : "Join class"}
        </button>
      </form>
      {message && (
        <p role="status" className={message.ok ? "stu-hint" : "stu-field-error"} style={{ margin: 0 }}>
          {message.text}
        </p>
      )}
    </section>
  );
}
