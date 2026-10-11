import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, NetworkError, slice6Api, studentApi } from "../../api/client";
import type { AnswerMap, AnswerValue, AttemptView, IntegrityEvent } from "../../api/types";
import { NotLockedBanner } from "../../components/LockBanner";
import { AnswerInput, RubricPanel } from "../../components/student/AnswerInput";
import { LockedBar } from "../../components/student/LockedBar";
import { ReviewPanel } from "../../components/student/ReviewPanel";
import { StuLoading } from "../../components/student/StudentLayout";
import { ArrowLeftIcon, ArrowRightIcon, ChevronIcon, CloseIcon, FlagIcon, GridIcon } from "../../components/student/icons";
import { AnswerQueue, type SyncStatus } from "../../lib/answerQueue";
import * as bridge from "../../lib/bridge";
import { clockOffset, remainingMs } from "../../lib/timer";
import {
  cacheAttemptView,
  cachedAttemptView,
  clearAttemptView,
  clearOfflineRecord,
  examIdOfOffline,
  isOfflineAttemptId,
  readOfflineRecord,
  saveOfflineRecord,
} from "../../lib/offline";
import { QuestionImages } from "../../components/QuestionImages";
import "../../styles/student.css";

export const HEARTBEAT_MS = 5000;
export const EVENT_RETRY_MS = 3000;
export const SUBMIT_RETRY_MS = 3000;

/** "Mark for review" flags are local only, kept per attempt in localStorage. */
export const marksStorageKey = (aid: string) => `proctora:marks:${aid}`;

export function readMarks(aid: string): Set<string> {
  try {
    const raw = localStorage.getItem(marksStorageKey(aid));
    const v = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function writeMarks(aid: string, marks: Set<string>): void {
  try {
    if (marks.size === 0) localStorage.removeItem(marksStorageKey(aid));
    else localStorage.setItem(marksStorageKey(aid), JSON.stringify([...marks]));
  } catch {
    /* storage unavailable: marks stay in memory */
  }
}

type Phase = "loading" | "locking" | "lockFailed" | "exam" | "review" | "submitting" | "loadError";

export interface DoneState {
  receipt: string;
  submitted_at: string;
  title: string;
  /** answered / total question counts at submit time (optional: unknown after a reload) */
  answered?: number;
  total?: number;
}

/** Remount per attempt id so no state leaks between attempts. */
export default function ExamTakingPage() {
  const { attemptId = "" } = useParams();
  return <ExamTakingInner key={attemptId} />;
}

function ExamTakingInner() {
  const { attemptId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();

  const [phase, setPhase] = useState<Phase>("loading");
  const [attempt, setAttempt] = useState<AttemptView | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [lockReason, setLockReason] = useState<string>("");
  const [answers, setAnswers] = useState<AnswerMap>({});
  const [sync, setSync] = useState<SyncStatus>("synced");
  const [current, setCurrent] = useState(0);
  const [deadline, setDeadline] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [locked, setLocked] = useState(bridge.isLocked());
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [marked, setMarked] = useState<Set<string>>(() => readMarks(attemptId));

  const queueRef = useRef<AnswerQueue | null>(null);
  const currentRef = useRef(0);
  currentRef.current = current;
  const finishedRef = useRef(false);
  const submittingRef = useRef(false);
  const eventsRef = useRef<IntegrityEvent[]>([]);
  const eventTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const submitRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const phaseBeforeSubmit = useRef<Phase>("exam");

  // ----- slice 6: offline attempts ("offline:<exam_id>", started from the package) -----
  // realAidRef is the server attempt id; null while an offline attempt has not reached the
  // server yet. Every API call goes through call(): online attempts call straight through,
  // offline ones first POST start with offline_started_at (once), then use the real id.
  const offline = isOfflineAttemptId(attemptId);
  const offlineExamId = offline ? examIdOfOffline(attemptId) : "";
  const realAidRef = useRef<string | null>(
    offline ? (readOfflineRecord(offlineExamId)?.real_attempt_id ?? null) : attemptId,
  );
  const [connected, setConnected] = useState(!!realAidRef.current);
  const [offlineProblem, setOfflineProblem] = useState<string | null>(null);
  const reconnectRef = useRef<Promise<string> | null>(null);

  const ensureOnline = useCallback((): Promise<string> => {
    if (realAidRef.current) return Promise.resolve(realAidRef.current);
    if (reconnectRef.current) return reconnectRef.current;
    const rec = readOfflineRecord(offlineExamId);
    if (!rec) return Promise.reject(new ApiError(404, "This exam attempt was not found."));
    const p = slice6Api
      .startAfterOffline(offlineExamId, rec.start_code, rec.started_at)
      .then(
        (view) => {
          realAidRef.current = view.attempt_id;
          saveOfflineRecord({ ...rec, real_attempt_id: view.attempt_id });
          setConnected(true);
          setOfflineProblem(null);
          setDeadline(view.deadline_at);
          setOffset(clockOffset(view.server_time));
          return view.attempt_id;
        },
        (e) => {
          if (e instanceof ApiError)
            setOfflineProblem(
              `${e.detail}. Your answers are kept on this computer — tell your teacher before closing this page.`,
            );
          throw e;
        },
      )
      .finally(() => {
        reconnectRef.current = null;
      });
    reconnectRef.current = p;
    return p;
  }, [offlineExamId]);

  const call = useCallback(
    <T,>(fn: (aid: string) => Promise<T>): Promise<T> => {
      const aid = realAidRef.current;
      return aid ? fn(aid) : ensureOnline().then(fn);
    },
    [ensureOnline],
  );

  // ----- finishing -----

  const finish = useCallback(
    (done: DoneState | null) => {
      if (finishedRef.current) return;
      finishedRef.current = true;
      queueRef.current?.stop();
      queueRef.current?.clearStorage();
      try {
        localStorage.removeItem(marksStorageKey(attemptId));
      } catch {
        /* ignore */
      }
      clearAttemptView(attemptId);
      const doneAid = realAidRef.current ?? attemptId;
      if (offline) {
        clearOfflineRecord(offlineExamId);
        clearAttemptView(doneAid);
      }
      bridge.unlock();
      navigate(`/student/done/${encodeURIComponent(doneAid)}`, { replace: true, state: done ?? undefined });
    },
    [attemptId, navigate, offline, offlineExamId],
  );

  const doSubmit = useCallback(
    async (auto: boolean) => {
      const q = queueRef.current;
      if (!q || finishedRef.current || submittingRef.current) return;
      submittingRef.current = true;
      if (submitRetryRef.current) clearTimeout(submitRetryRef.current);
      setPhase((p) => {
        if (p !== "submitting") phaseBeforeSubmit.current = p;
        return "submitting";
      });
      setSubmitError(null);
      try {
        const sent = q.getAnswers();
        const seq = q.getClientSeq();
        const res = await call((aid) => studentApi.submit(aid, sent, seq));
        submittingRef.current = false;
        const total = attempt?.questions.length;
        const answered = attempt ? attempt.questions.filter((qq) => sent[qq.id] !== undefined).length : undefined;
        finish({ receipt: res.receipt, submitted_at: res.submitted_at, title: attempt?.title ?? "", answered, total });
      } catch (e) {
        submittingRef.current = false;
        const msg =
          e instanceof NetworkError
            ? "Cannot reach the server. Your answers are saved on this computer."
            : e instanceof ApiError
              ? e.detail
              : "Submitting failed.";
        if (auto || e instanceof NetworkError) {
          setSubmitError(`${msg} Trying again…`);
          submitRetryRef.current = setTimeout(() => void doSubmit(auto), SUBMIT_RETRY_MS);
        } else {
          setSubmitError(msg);
          setPhase(phaseBeforeSubmit.current);
        }
      }
    },
    [attempt, finish, call],
  );
  const doSubmitRef = useRef(doSubmit);
  doSubmitRef.current = doSubmit;

  // ----- load attempt (from start navigation or GET for resume) -----

  useEffect(() => {
    let alive = true;
    const fromState = (location.state as { attempt?: AttemptView } | null)?.attempt;
    let load: Promise<AttemptView>;
    if (fromState && fromState.attempt_id === attemptId) load = Promise.resolve(fromState);
    else if (offline) {
      // reload during an offline attempt: the local copy is the source (local clock)
      const rec = readOfflineRecord(offlineExamId);
      load = rec
        ? Promise.resolve({ ...rec.view, server_time: new Date().toISOString() })
        : Promise.reject(new ApiError(404, "This exam attempt was not found."));
    } else
      load = studentApi.getAttempt(attemptId).catch((e: unknown) => {
        // slice 6: resume from the last copy on this computer when the server is unreachable
        const cached = e instanceof NetworkError ? cachedAttemptView(attemptId) : null;
        if (cached) return cached;
        throw e;
      });
    load.then(
      (a) => {
        if (!alive) return;
        if (!offline && a.status !== "submitted") cacheAttemptView(a, clockOffset(a.server_time));
        if (a.mode === "external") {
          // Exams held on another website run in their own page (CONTRACT slice 3).
          navigate(`/student/external/${encodeURIComponent(attemptId)}`, { replace: true, state: { attempt: a } });
          return;
        }
        if (a.status === "submitted") {
          finish(null);
          return;
        }
        const queue = new AnswerQueue(attemptId, (ans, seq) => call((aid) => studentApi.saveAnswers(aid, ans, seq)), {
          onClosed: () => void doSubmitRef.current(true),
        });
        queue.subscribe((s) => {
          setSync(s);
          setAnswers(queue.getAnswers());
        });
        queueRef.current = queue;
        queue.init(a.answers);
        setAnswers(queue.getAnswers());
        setAttempt(a);
        setDeadline(a.deadline_at);
        setOffset(clockOffset(a.server_time));
        setPhase("locking");
      },
      (e) => {
        if (!alive) return;
        setLoadError(
          e instanceof ApiError && e.status === 404
            ? "This exam attempt was not found."
            : e instanceof ApiError
              ? e.detail
              : "Cannot reach the server. Check the connection and reload the page.",
        );
        setPhase("loadError");
      },
    );
    return () => {
      alive = false;
      queueRef.current?.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attemptId]);

  // ----- lock before showing questions -----

  const lockingRef = useRef(false);
  const lock = useCallback(async () => {
    if (lockingRef.current) return;
    lockingRef.current = true;
    setPhase("locking");
    const res = await bridge.requestLock();
    lockingRef.current = false;
    if (finishedRef.current) return;
    if (res.ok) {
      setLocked(bridge.isLocked());
      setPhase("exam");
    } else {
      setLockReason(res.reason);
      setPhase("lockFailed");
    }
  }, []);

  useEffect(() => {
    if (phase === "locking" && attempt) void lock();
    // only when we enter "locking" after load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  // ----- native messages: lock state + integrity events -----

  const flushEvents = useCallback(async () => {
    if (eventTimerRef.current) {
      clearTimeout(eventTimerRef.current);
      eventTimerRef.current = null;
    }
    const batch = eventsRef.current;
    if (batch.length === 0) return;
    eventsRef.current = [];
    try {
      await call((aid) => studentApi.events(aid, batch));
    } catch (e) {
      if (e instanceof ApiError && e.status !== 429 && e.status < 500) return; // rejected: drop
      eventsRef.current = [...batch, ...eventsRef.current];
      eventTimerRef.current = setTimeout(() => void flushEvents(), EVENT_RETRY_MS);
    }
  }, [call]);

  useEffect(() => {
    const off = bridge.onNative((msg) => {
      if (msg.type === "state" || msg.type === "locked" || msg.type === "unlocked" || msg.type === "lockFailed") {
        setLocked(bridge.isLocked());
      }
      if (msg.type === "event" && msg.event) {
        eventsRef.current.push({
          type: String(msg.event.type),
          detail: String(msg.event.detail ?? ""),
          client_time: msg.event.client_time ?? new Date().toISOString(),
        });
        void flushEvents();
      }
    });
    return () => {
      off();
      if (eventTimerRef.current) clearTimeout(eventTimerRef.current);
    };
  }, [flushEvents]);

  // ----- heartbeat every 5 s -----

  useEffect(() => {
    if (!attempt) return;
    const beat = async () => {
      if (finishedRef.current) return;
      try {
        const res = await call((aid) => studentApi.heartbeat(aid, currentRef.current, bridge.isLocked()));
        setOffset(clockOffset(res.server_time));
        if (res.deadline_at) setDeadline(res.deadline_at);
        if (res.status === "submitted") {
          // closed by the teacher (or submitted elsewhere): finish and unlock
          if (!submittingRef.current) void doSubmitRef.current(true);
        }
      } catch {
        /* offline: keep going, answers stay on this computer */
      }
    };
    void beat();
    const t = setInterval(beat, HEARTBEAT_MS);
    return () => clearInterval(t);
  }, [attempt, attemptId, call]);

  // ----- countdown + auto submit at 0 -----

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => {
      clearInterval(t);
      if (submitRetryRef.current) clearTimeout(submitRetryRef.current);
    };
  }, []);

  const remaining = deadline ? remainingMs(deadline, offset, now) : Infinity;
  useEffect(() => {
    if (!attempt || !deadline) return;
    if (remaining <= 0 && (phase === "exam" || phase === "review" || phase === "lockFailed")) {
      void doSubmit(true);
    }
  }, [remaining, phase, attempt, deadline, doSubmit]);

  // ----- answering -----

  const setAnswer = useCallback((qid: string, value: AnswerValue | undefined) => {
    queueRef.current?.set(qid, value);
    if (queueRef.current) setAnswers(queueRef.current.getAnswers());
  }, []);

  const questions = attempt?.questions ?? [];
  const unanswered = useMemo(
    () => questions.map((q, i) => ({ q, i })).filter(({ q }) => answers[q.id] === undefined),
    [questions, answers],
  );


  // ----- mark for review (local only) -----

  const toggleMark = useCallback(
    (qid: string) => {
      setMarked((prev) => {
        const next = new Set(prev);
        if (next.has(qid)) next.delete(qid);
        else next.add(qid);
        writeMarks(attemptId, next);
        return next;
      });
    },
    [attemptId],
  );

  // Move focus to the question when the student changes question (not on first show).
  const promptRef = useRef<HTMLHeadingElement>(null);
  const shownRef = useRef(false);
  useEffect(() => {
    if (phase !== "exam") return;
    if (!shownRef.current) {
      shownRef.current = true;
      return;
    }
    promptRef.current?.focus();
  }, [current, phase]);

  const goTo = (i: number) => {
    setCurrent(i);
    setPhase("exam");
  };

  // ----- question navigator panel (opens from the bottom bar) -----

  const [navOpen, setNavOpen] = useState(false);
  const navToggleRef = useRef<HTMLButtonElement>(null);
  const navPanelRef = useRef<HTMLDivElement>(null);
  const closeNav = useCallback((restoreFocus: boolean) => {
    setNavOpen(false);
    if (restoreFocus) navToggleRef.current?.focus();
  }, []);
  useEffect(() => {
    if (phase !== "exam") setNavOpen(false);
  }, [phase]);
  useEffect(() => {
    if (!navOpen) return;
    // focus the current question's cell so arrow/Tab navigation starts there
    navPanelRef.current?.querySelector<HTMLElement>("[aria-current=step]")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeNav(true);
    };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (navPanelRef.current?.contains(t) || navToggleRef.current?.contains(t)) return;
      closeNav(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [navOpen, closeNav]);

  // ----- render -----

  if (phase === "loading")
    return (
      <div className="stu stu-exam">
        <div className="stu-center">
          <StuLoading label="Loading your exam…" />
        </div>
      </div>
    );
  if (phase === "loadError")
    return (
      <div className="stu stu-exam">
        <main className="stu-center">
          <div className="stu-card">
            <h1 style={{ margin: 0, fontSize: 22 }}>Cannot open the exam</h1>
            <p className="stu-muted">{loadError}</p>
            <div className="stu-row">
              <button type="button" className="stu-btn primary" onClick={() => window.location.reload()}>
                Try again
              </button>
              <Link to="/student" className="stu-btn">
                Back to home
              </Link>
            </div>
          </div>
        </main>
      </div>
    );
  if (!attempt) return null;

  const q = questions[current];
  const answeredCount = questions.length - unanswered.length;
  const markedCount = questions.filter((qq) => marked.has(qq.id)).length;
  const isMarked = q ? marked.has(q.id) : false;
  const showExam = phase === "exam" || (phase === "submitting" && phaseBeforeSubmit.current === "exam");
  const showReview = phase === "review" || (phase === "submitting" && phaseBeforeSubmit.current !== "exam");
  const typeLabel = q?.type === "essay" ? " · Essay" : "";

  const isLast = current === questions.length - 1;

  return (
    <div className="stu stu-exam">
      <NotLockedBanner />
      <LockedBar
        title={attempt.title}
        course={attempt.course}
        locked={bridge.isNative() && locked}
        native={bridge.isNative()}
        sync={sync}
        remaining={remaining}
      />
      {offline && (offlineProblem || !connected) && (
        <div className="stu-alert warn fp-offline-note" role="status" data-testid="offline-banner">
          {offlineProblem ??
            "Working offline. Your answers are kept on this computer and sent when the connection is back."}
        </div>
      )}

      {phase === "locking" && (
        <div className="stu-center">
          <StuLoading label="Locking this computer for the exam…" />
        </div>
      )}

      {phase === "lockFailed" && (
        <main className="stu-center">
          <div className="stu-card" style={{ gap: 14 }}>
            <h1 style={{ margin: 0, fontSize: 22 }}>Locked mode could not start</h1>
            <p style={{ margin: 0, color: "var(--s-text-2)" }}>
              {lockReason === "accessibility"
                ? "FairPlay needs the Accessibility permission to lock this computer."
                : "The FairPlay app could not lock this computer."}
            </p>
            <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 1.7, color: "var(--s-text-2)" }}>
              <li>Open System Settings → Privacy &amp; Security → Accessibility.</li>
              <li>Turn on FairPlay.</li>
              <li>Come back here and press Retry.</li>
            </ol>
            <p className="stu-muted stu-small" style={{ margin: 0 }}>
              Your exam time is running. Ask your teacher if you need help.
            </p>
            <div>
              <button type="button" className="stu-btn primary lg" onClick={() => void lock()}>
                Retry
              </button>
            </div>
          </div>
        </main>
      )}

      {showExam && q && (
        <>
          <div className="stu-exam-body">
            <main className="stu-paper stu-question" aria-labelledby="question-prompt">
              <div className="stu-question-meta">
                <span>
                  <b>
                    Question {current + 1} of {questions.length}
                  </b>
                  {typeLabel} · {q.points} {q.points === 1 ? "point" : "points"}
                </span>
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={isMarked}
                  className="stu-mark"
                  onClick={() => toggleMark(q.id)}
                >
                  <FlagIcon />
                  {isMarked ? "Marked for review" : "Mark for review"}
                </button>
              </div>

              <QuestionImages images={q.images} />

              <h1 id="question-prompt" ref={promptRef} tabIndex={-1} className="stu-prompt">
                {q.prompt}
              </h1>

              {q.type === "essay" && <RubricPanel question={q} />}

              <AnswerInput
                key={q.id}
                question={q}
                value={answers[q.id]}
                onChange={(v) => setAnswer(q.id, v)}
                labelledBy="question-prompt"
              />
            </main>
          </div>

          <nav className="stu-bottombar" aria-label="Questions">
            <div className="stu-bottombar-inner">
              <button
                type="button"
                className="stu-btn lg"
                onClick={() => setCurrent((c) => Math.max(0, c - 1))}
                disabled={current === 0}
              >
                <ArrowLeftIcon />
                Previous
              </button>

              <button
                ref={navToggleRef}
                type="button"
                className="stu-qtoggle"
                aria-expanded={navOpen}
                aria-controls="question-navigator"
                onClick={() => setNavOpen((o) => !o)}
              >
                <GridIcon />
                Question {current + 1} of {questions.length}
                <ChevronIcon className="chev" />
              </button>

              {isLast ? (
                <button type="button" className="stu-btn primary lg" onClick={() => setPhase("review")}>
                  Review and submit
                </button>
              ) : (
                <button type="button" className="stu-btn primary lg" onClick={() => setCurrent((c) => c + 1)}>
                  Next
                  <ArrowRightIcon />
                </button>
              )}

              {navOpen && (
                <div
                  ref={navPanelRef}
                  id="question-navigator"
                  className="stu-navigator"
                  role="dialog"
                  aria-labelledby="nav-h"
                >
                  <div className="stu-navigator-head">
                    <h2 id="nav-h">All questions</h2>
                    <button type="button" className="stu-icon-btn" aria-label="Close" onClick={() => closeNav(true)}>
                      <CloseIcon />
                    </button>
                  </div>
                  <div className="stu-qgrid">
                    {questions.map((qq, i) => {
                      const isAnswered = answers[qq.id] !== undefined;
                      const m = marked.has(qq.id);
                      const isCurrent = i === current;
                      const state = [isAnswered ? "answered" : "not answered", m && "marked for review", isCurrent && "current"]
                        .filter(Boolean)
                        .join(", ");
                      return (
                        <button
                          key={qq.id}
                          type="button"
                          className={`stu-qcell${isAnswered ? " answered" : ""}${m ? " marked" : ""}${isCurrent ? " current" : ""}`}
                          aria-label={`Question ${i + 1}, ${state}`}
                          aria-current={isCurrent ? "step" : undefined}
                          onClick={() => {
                            setCurrent(i);
                            closeNav(false);
                          }}
                        >
                          {i + 1}
                        </button>
                      );
                    })}
                  </div>
                  <div className="stu-legend">
                    <span>
                      <span className="stu-swatch answered" aria-hidden="true" />
                      Answered {answeredCount}
                    </span>
                    <span>
                      <span className="stu-swatch marked" aria-hidden="true" />
                      Marked {markedCount}
                    </span>
                    <span>
                      <span className="stu-swatch" aria-hidden="true" />
                      Not yet {unanswered.length}
                    </span>
                  </div>
                  <button type="button" className="stu-btn block" onClick={() => setPhase("review")}>
                    Review and submit
                  </button>
                  <p className="stu-offline-note">
                    If Wi-Fi drops, keep going. Answers are stored on this computer and sent when you&apos;re back online.
                  </p>
                </div>
              )}
            </div>
          </nav>
        </>
      )}

      {showReview && (
        <ReviewPanel
          questions={questions}
          answers={answers}
          marked={marked}
          onGoTo={goTo}
          onKeepWorking={() => setPhase("exam")}
          onSubmit={() => void doSubmit(false)}
          submitting={phase === "submitting"}
          error={submitError}
        />
      )}

      {phase === "submitting" && phaseBeforeSubmit.current === "exam" && (
        <div className="stu-toast" role="status">
          {submitError ?? "Time is up. Submitting your answers…"}
        </div>
      )}
    </div>
  );
}
