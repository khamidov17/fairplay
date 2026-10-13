// Slice 3: external-website exam (SEB-style). CONTRACT.md "Web routes (added)".
// Loads the attempt; outside the Mac app explains that the exam opens only in the app;
// inside it locks the computer, then asks the app to open the exam website (openExternal).
// From then on the Mac app owns the screen until it loads /student/external-done/:attemptId.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, getToken, studentApi } from "../../api/client";
import type { AttemptView } from "../../api/types";
import * as bridge from "../../lib/bridge";
import { getAttemptSession } from "../../lib/appSession";
import { useForwardNativeEvents } from "../../lib/useForwardNativeEvents";
import { clockOffset, remainingMs } from "../../lib/timer";
import { ReenterExam, isSessionConflict } from "../../components/student/ReenterExam";
import { PreflightProblems } from "../../components/student/Preflight";
import "../../styles/external.css";
import "../../styles/student.css";

type Phase = "loading" | "loadError" | "reenter" | "notNative" | "locking" | "preflight" | "lockFailed" | "opening" | "openFailed";

export default function ExternalExamPage() {
  const { attemptId = "" } = useParams();
  const navigate = useNavigate();
  useForwardNativeEvents(attemptId);
  const [phase, setPhase] = useState<Phase>("loading");
  const [attempt, setAttempt] = useState<AttemptView | null>(null);
  const [loadError, setLoadError] = useState("");
  const [lockReason, setLockReason] = useState("");
  const [preflightProblems, setPreflightProblems] = useState<string[]>([]);
  // server clock offset measured when the attempt arrived
  const offsetRef = useRef(0);
  const lockingRef = useRef(false);

  // ----- load -----
  useEffect(() => {
    let alive = true;
    bridge.hello();
    studentApi.getAttempt(attemptId).then(
      (a) => {
        if (!alive) return;
        if (a.status === "submitted") {
          navigate(`/student/done/${encodeURIComponent(attemptId)}`, { replace: true });
          return;
        }
        if (a.mode !== "external") {
          navigate(`/student/exam/${encodeURIComponent(attemptId)}`, { replace: true });
          return;
        }
        offsetRef.current = clockOffset(a.server_time);
        setAttempt(a);
        setPhase(bridge.isNative() ? "locking" : "notNative");
      },
      (e: unknown) => {
        if (!alive) return;
        if (e instanceof ApiError && isSessionConflict(e.status, e.detail)) {
          setLoadError(e.detail);       // audit W-01
          setPhase("reenter");
          return;
        }
        // Audit W-11: say what the server said; only a network failure is "cannot reach".
        setLoadError(
          e instanceof ApiError
            ? e.status === 404
              ? "This exam attempt was not found."
              : e.detail
            : "Cannot reach the server. Check the connection and try again.",
        );
        setPhase("loadError");
      },
    );
    return () => {
      alive = false;
    };
  }, [attemptId, navigate]);

  // ----- open the website inside the lockdown -----
  const open = useCallback(
    (a: AttemptView) => {
      if (remainingMs(a.deadline_at, offsetRef.current) <= 0) {
        // Time already ran out: hand in straight away.
        navigate(`/student/external-done/${encodeURIComponent(a.attempt_id)}`, { replace: true });
        return;
      }
      const token = getToken("student");
      const session = getAttemptSession(a.attempt_id);
      if (!session) {
        // Audit W-01: no app session for this attempt on this computer: start code again.
        setLoadError("This computer has no session for this exam.");
        setPhase("reenter");
        return;
      }
      if (!token || !a.external_url) {
        setPhase("openFailed");
        return;
      }
      setPhase("opening");
      const sent = bridge.openExternal({
        attempt_id: a.attempt_id,
        url: a.external_url,
        allowed_domains: a.allowed_domains ?? [],
        finish_url_contains: a.finish_url_contains ?? null,
        deadline_at: a.deadline_at,
        // Re-stamp with the server clock now (the attempt may have loaded a while ago).
        server_time: new Date(Date.now() + offsetRef.current).toISOString(),
        title: a.course ? `${a.course} ${a.title}` : a.title,
        token,
        session,
      });
      if (!sent) setPhase("openFailed");
    },
    [navigate],
  );

  const lock = useCallback(async () => {
    if (!attempt || lockingRef.current) return;
    lockingRef.current = true;
    setPhase("locking");
    const problems = await bridge.preflight();          // audit M-11
    if (problems.length > 0) {
      lockingRef.current = false;
      setPreflightProblems(problems);
      setPhase("preflight");
      return;
    }
    const token = getToken("student");
    const session = getAttemptSession(attempt.attempt_id);
    const res = await bridge.requestLock(
      10000,
      token && session ? { attempt_id: attempt.attempt_id, token, session } : undefined,
    );
    lockingRef.current = false;
    if (res.ok) open(attempt);
    else {
      setLockReason(res.reason);
      setPhase("lockFailed");
    }
  }, [attempt, open]);

  useEffect(() => {
    if (phase === "locking" && attempt && !lockingRef.current) void lock();
    // only when we enter "locking" after the attempt loaded
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  // ----- render -----
  const title = attempt ? (attempt.course ? `${attempt.course} · ${attempt.title}` : attempt.title) : "";

  if (phase === "loading" || phase === "locking" || phase === "opening") {
    const label =
      phase === "loading"
        ? "Loading your exam…"
        : phase === "locking"
          ? "Locking this computer for the exam…"
          : "Opening the exam website…";
    return (
      <div className="ext-page">
        <main className="ext-card" aria-busy="true">
          {title && <p className="ext-eyebrow">{title}</p>}
          <div className="ext-status" role="status">
            <span className="ext-spinner" aria-hidden="true" />
            <span>{label}</span>
          </div>
          {phase === "opening" && (
            <p className="ext-small">
              The exam website opens in this window. Use “Finish exam” at the top when you are done.
            </p>
          )}
        </main>
      </div>
    );
  }

  if (phase === "reenter") return <ReenterExam attemptId={attemptId} detail={loadError} />;
  if (phase === "preflight")
    return (
      <div className="stu stu-exam">
        <PreflightProblems problems={preflightProblems} onRetry={() => void lock()} />
      </div>
    );
  if (phase === "loadError")
    return (
      <div className="ext-page">
        <main className="ext-card">
          <h1>Cannot open the exam</h1>
          <p>{loadError}</p>
          <div className="ext-row">
            <button type="button" className="ext-btn primary" onClick={() => window.location.reload()}>
              Try again
            </button>
            <Link to="/student" className="ext-btn">
              Back to home
            </Link>
          </div>
        </main>
      </div>
    );

  if (phase === "notNative")
    return (
      <div className="ext-page">
        <main className="ext-card">
          {title && <p className="ext-eyebrow">{title}</p>}
          <h1>This exam opens only in the FairPlay app</h1>
          <p>
            Your teacher runs this exam on another website. For a fair exam it can only be opened in the FairPlay
            app on your Mac, which locks the computer while you work.
          </p>
          <ol style={{ paddingLeft: 20 }}>
            <li>Open the FairPlay app and sign in.</li>
            <li>Find this exam and enter the start code again.</li>
          </ol>
          <p className="ext-small">Your exam time has started. Ask your teacher if you need help.</p>
          <div className="ext-row">
            <Link to="/student" className="ext-btn">
              Back to home
            </Link>
          </div>
        </main>
      </div>
    );

  if (phase === "lockFailed")
    return (
      <div className="ext-page">
        <main className="ext-card">
          {title && <p className="ext-eyebrow">{title}</p>}
          <h1>Locked mode could not start</h1>
          <p>
            {lockReason === "accessibility"
              ? "FairPlay needs the Accessibility permission to lock this computer."
              : "The FairPlay app could not lock this computer."}
          </p>
          <ol style={{ paddingLeft: 20 }}>
            <li>Open System Settings → Privacy &amp; Security → Accessibility.</li>
            <li>Turn on FairPlay.</li>
            <li>Come back here and press Retry.</li>
          </ol>
          <p className="ext-small">Your exam time is running. Ask your teacher if you need help.</p>
          <div className="ext-row">
            <button type="button" className="ext-btn primary" onClick={() => void lock()}>
              Retry
            </button>
          </div>
        </main>
      </div>
    );

  // openFailed
  return (
    <div className="ext-page">
      <main className="ext-card">
        {title && <p className="ext-eyebrow">{title}</p>}
        <h1>The exam website could not be opened</h1>
        <p>Sign in again in the FairPlay app, then open this exam once more.</p>
        <div className="ext-row">
          <button type="button" className="ext-btn primary" onClick={() => attempt && void lock()}>
            Try again
          </button>
          <Link to="/student" className="ext-btn">
            Back to home
          </Link>
        </div>
      </main>
    </div>
  );
}
