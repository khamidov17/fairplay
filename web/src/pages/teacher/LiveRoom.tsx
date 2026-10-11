import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { NetworkError, teacherApi } from "../../api/client";
import type { Exam, LiveFlag, LiveRoom as LiveRoomData, LiveStudent } from "../../api/types";
import { errorText, eventLabel, plural } from "../../components/teacher/util";
import { formatClock, formatCountdown } from "../../lib/timer";

export const LIVE_POLL_MS = 3000;

export type SeatState = "writing" | "done" | "offline" | "look";

export function seatState(s: LiveStudent): SeatState {
  if (s.needs_look) return "look";
  if (s.status === "submitted") return "done";
  if (!s.online) return "offline";
  return "writing";
}

const SEAT_LABEL: Record<SeatState, string> = { writing: "Writing", done: "Finished", offline: "Offline", look: "Needs your look" };

function seatSub(s: LiveStudent, st: SeatState): string {
  if (s.status === "submitted") return st === "look" ? "Done · look" : "Done";
  if (st === "offline") return `Q${s.current_question + 1} · saved`;
  if (st === "look") return `Q${s.current_question + 1} · look`;
  return `Q${s.current_question + 1} · ${s.answered}/${s.question_count}`;
}

/** Fallback when the server has not sent `counts` yet. */
export function countsOf(live: LiveRoomData): LiveRoomData["counts"] {
  if (live.counts) return live.counts;
  const st = live.students;
  return {
    writing: st.filter((s) => s.status === "in_progress" && s.online).length,
    submitted: st.filter((s) => s.status === "submitted").length,
    offline: st.filter((s) => s.status === "in_progress" && !s.online).length,
    needs_look: st.filter((s) => s.needs_look).length,
  };
}

/** Time until the last writing student must finish, or null if nobody is writing. */
export function latestDeadlineMs(students: LiveStudent[], now: number): number | null {
  const writing = students.filter((s) => s.status === "in_progress");
  if (writing.length === 0) return null;
  const latest = Math.max(...writing.map((s) => new Date(s.deadline_at).getTime()).filter(Number.isFinite));
  return Number.isFinite(latest) ? Math.max(0, latest - now) : null;
}

interface Props {
  exam: Exam;
  onClosed: (exam: Exam) => void;
  onExamChange: (exam: Exam) => void;
}

export function LiveRoom({ exam, onClosed, onExamChange }: Props) {
  const [live, setLive] = useState<LiveRoomData | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [closing, setClosing] = useState(false);
  const [extending, setExtending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  const load = useCallback(async () => {
    try {
      const data = await teacherApi.live(exam.id);
      setLive(data);
      setPollError(null);
    } catch (e) {
      setPollError(e instanceof NetworkError ? "Connection lost. Retrying every 3 seconds…" : errorText(e));
    }
  }, [exam.id]);

  useEffect(() => {
    void load();
    const poll = setInterval(load, LIVE_POLL_MS);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [load]);

  async function close() {
    setClosing(true);
    setActionError(null);
    try {
      const updated = await teacherApi.closeExam(exam.id);
      setConfirming(false);
      onClosed(updated);
    } catch (e) {
      setActionError(errorText(e));
    } finally {
      setClosing(false);
    }
  }

  async function extend() {
    setExtending(true);
    setActionError(null);
    try {
      const updated = await teacherApi.extend(exam.id, 10);
      onExamChange(updated);
      await load();
    } catch (e) {
      setActionError(errorText(e));
    } finally {
      setExtending(false);
    }
  }

  const students = live?.students ?? [];
  const counts = live ? countsOf(live) : { writing: 0, submitted: 0, offline: 0, needs_look: 0 };
  const flags = live?.flags ?? [];
  const code = live?.start_code ?? exam.start_code;
  // phase 1a: per-exam invigilator exit code (Ctrl-Opt-Cmd-E on a locked Mac) — teacher only
  const exitCode = live?.exit_code ?? exam.exit_code ?? null;
  const left = latestDeadlineMs(students, now);
  const stillWriting = counts.writing + counts.offline;
  const sub = [exam.course, exam.room && `Room ${exam.room}`, plural(students.length, "student")].filter(Boolean).join(" · ");

  return (
    <>
      <section className="t-live-head" aria-label="Live exam controls">
        <div className="t-live-title">
          <h2>{exam.title}</h2>
          <span>{sub}</span>
        </div>
        <div className="t-dark-box">
          <span className="t-dark-box-label">Start code · read it aloud</span>
          <div className="t-code">
            {code ?? "----"}
          </div>
        </div>
        {exitCode && (
          <div className="t-dark-box">
            <span className="t-dark-box-label">Invigilator exit code · do not read aloud</span>
            <div className="t-live-time" data-testid="exit-code" title="Unlocks a student's Mac in an emergency (Ctrl-Opt-Cmd-E). Every use is logged.">
              {exitCode}
            </div>
          </div>
        )}
        <div className="t-dark-box">
          <span className="t-dark-box-label">{left == null ? "Each student gets" : "Time left"}</span>
          <div className="t-live-time" title={left == null ? undefined : "Until the last student who is writing must finish"}>
            {left == null ? formatCountdown(exam.duration_min * 60_000) : formatCountdown(left)}
          </div>
        </div>
        <div className="t-live-actions">
          <button type="button" className="t-btn-dark-outline" onClick={extend} disabled={extending || closing}>
            {extending ? "Adding…" : "Add 10 min"}
          </button>
          {!confirming && (
            <button type="button" className="t-btn-dark-outline t-btn-dark-danger" onClick={() => setConfirming(true)}>
              Close exam
            </button>
          )}
        </div>
        {confirming && (
          <div className="t-confirm-dark" role="alertdialog" aria-labelledby="close-q">
            <p id="close-q">
              <strong>Close this exam now?</strong>{" "}
              {stillWriting > 0
                ? `${plural(stillWriting, "student")} still writing will be submitted automatically.`
                : "Everyone has finished."}{" "}
              The start code stops working.
            </p>
            <button type="button" className="t-btn-light t-btn-solid-danger" onClick={close} disabled={closing}>
              {closing ? "Closing…" : "Yes, close exam"}
            </button>
            <button type="button" className="t-btn-dark-outline" onClick={() => setConfirming(false)} disabled={closing}>
              Cancel
            </button>
          </div>
        )}
      </section>
      {actionError && (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      )}

      <div className="t-columns">
        <section aria-labelledby="seat-h" className="t-aside" style={{ flex: "999 1 560px", minWidth: 0, gap: 18 }}>
          <div className="t-stats" aria-label="Room summary">
            <div className="t-stat">
              <span className="t-stat-value blue">{counts.writing}</span>
              <span className="t-stat-label">Writing</span>
            </div>
            <div className="t-stat">
              <span className="t-stat-value green">{counts.submitted}</span>
              <span className="t-stat-label">Finished</span>
            </div>
            <div className="t-stat">
              <span className="t-stat-value grey">{counts.offline}</span>
              <span className="t-stat-label">Offline · answers saved</span>
            </div>
            <div className="t-stat warn">
              <span className="t-stat-value orange">{counts.needs_look}</span>
              <span className="t-stat-label">Need your look</span>
            </div>
          </div>

          <div className="t-card">
            <div className="t-card-head">
              <h2 id="seat-h">Students</h2>
              <div className="t-legend" aria-label="Legend">
                <span>
                  <i className="t-swatch writing" />
                  Writing
                </span>
                <span>
                  <i className="t-swatch done" />
                  Finished
                </span>
                <span>
                  <i className="t-swatch offline" />
                  Offline
                </span>
                <span>
                  <i className="t-swatch look" />
                  Look
                </span>
              </div>
            </div>
            <span className="t-small" aria-live="polite" style={{ color: pollError ? "var(--warn)" : "var(--muted)" }}>
              {pollError ?? "Updates every 3 seconds"}
            </span>
            {live === null && !pollError ? (
              <div className="t-empty">Loading students…</div>
            ) : students.length === 0 ? (
              <div className="t-empty">
                <strong>No students yet</strong>
                Students appear here as soon as they enter the start code.
              </div>
            ) : (
              <ul className="t-seats" aria-label="Students in the room">
                {students.map((s) => {
                  const st = seatState(s);
                  const desc = `${s.name}: ${SEAT_LABEL[st]}, ${s.answered} of ${s.question_count} answered${
                    s.status === "in_progress" ? `, on question ${s.current_question + 1}` : ""
                  }${s.locked ? "" : ", not locked"}`;
                  return (
                    <li key={s.attempt_id} className={`t-seat ${st}`} title={desc}>
                      <Link
                        to={`/teacher/exams/${exam.id}/submissions/${s.attempt_id}`}
                        className="t-seat-link"
                        aria-label={`${desc}. Open submission`}
                      >
                        <span className="t-seat-name">{s.name}</span>
                        <span className="t-seat-sub">{seatSub(s, st)}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </section>

        <FlagsPanel examId={exam.id} flags={flags} onReviewed={load} />
      </div>
    </>
  );
}

function FlagsPanel({ examId, flags, onReviewed }: { examId: string; flags: LiveFlag[]; onReviewed: () => void }) {
  const [hidden, setHidden] = useState<Set<number>>(new Set());
  const shown = flags.filter((f) => !hidden.has(f.event_id));
  return (
    <aside aria-labelledby="flag-h" className="t-card" style={{ flex: "1 1 340px", minWidth: 0 }}>
      <h2 id="flag-h">Needs your look</h2>
      <p className="t-small t-muted" style={{ margin: 0, lineHeight: 1.5 }}>
        The system only suggests. You decide — nothing is reported until you confirm.
      </p>
      {shown.length === 0 ? (
        <div className="t-empty">
          <strong>Nothing to look at</strong>
          Blocked shortcuts and other lockdown events show up here.
        </div>
      ) : (
        shown.map((f) => (
          <FlagCard
            key={f.event_id}
            examId={examId}
            flag={f}
            onDone={() => {
              setHidden((h) => new Set(h).add(f.event_id));
              onReviewed();
            }}
          />
        ))
      )}
    </aside>
  );
}

function FlagCard({ examId, flag, onDone }: { examId: string; flag: LiveFlag; onDone: () => void }) {
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteId = `flag-note-${flag.event_id}`;

  async function review(status: "dismissed" | "noted") {
    setBusy(true);
    setError(null);
    try {
      await teacherApi.reviewEvent(examId, flag.event_id, status, status === "noted" ? note.trim() : "");
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <article className="t-flag" aria-label={`${flag.name}: ${eventLabel(flag.type)}`}>
      <div className="t-flag-top">
        <span className="t-flag-who">
          {flag.name} · {eventLabel(flag.type)}
        </span>
        <span className="t-muted">{formatClock(flag.server_time)}</span>
      </div>
      {flag.detail && <span className="t-flag-detail mono">{flag.detail}</span>}
      {noting && (
        <div className="t-field">
          <label htmlFor={noteId}>Note for the record</label>
          <textarea
            id={noteId}
            className="textarea"
            rows={2}
            maxLength={500}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            autoFocus
          />
        </div>
      )}
      {error && <span className="t-inline-error">{error}</span>}
      <div className="t-flag-actions">
        {noting ? (
          <>
            <button type="button" className="t-btn-outline" onClick={() => setNoting(false)} disabled={busy}>
              Cancel
            </button>
            <button type="button" className="t-btn-warn" onClick={() => review("noted")} disabled={busy || !note.trim()}>
              Save note
            </button>
          </>
        ) : (
          <>
            <button type="button" className="t-btn-outline" onClick={() => review("dismissed")} disabled={busy}>
              Looks fine
            </button>
            <button type="button" className="t-btn-outline" onClick={() => setNoting(true)} disabled={busy}>
              Add note
            </button>
          </>
        )}
      </div>
    </article>
  );
}
