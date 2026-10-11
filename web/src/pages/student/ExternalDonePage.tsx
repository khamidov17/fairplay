// Slice 3: the Mac app loads this page when an external-website exam finishes
// (Finish button, finish URL, deadline or teacher closing). It hands the attempt in
// (idempotent), unlocks the computer and shows the normal receipt page.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, studentApi } from "../../api/client";
import * as bridge from "../../lib/bridge";
import { useForwardNativeEvents } from "../../lib/useForwardNativeEvents";
import "../../styles/external.css";

export const EXTERNAL_SUBMIT_RETRY_MS = 3000;

export default function ExternalDonePage() {
  const { attemptId = "" } = useParams();
  const navigate = useNavigate();
  useForwardNativeEvents(attemptId);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);
  const titleRef = useRef("");

  const submit = useCallback(async () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setError(null);
    try {
      const res = await studentApi.submit(attemptId, {}, 0);
      if (!aliveRef.current) return;
      bridge.unlock();
      navigate(`/student/done/${encodeURIComponent(attemptId)}`, {
        replace: true,
        state: { receipt: res.receipt, submitted_at: res.submitted_at, title: titleRef.current },
      });
    } catch (e) {
      if (!aliveRef.current) return;
      if (e instanceof ApiError && e.status !== 429 && e.status < 500) {
        // Rejected (e.g. not this student's attempt): retrying will not help.
        setError(e.status === 404 ? "This exam attempt was not found." : e.detail || "The exam could not be handed in.");
        setRetrying(false);
        return;
      }
      // Offline / server busy: keep trying; the computer stays locked meanwhile.
      setRetrying(true);
      timerRef.current = setTimeout(() => void submit(), EXTERNAL_SUBMIT_RETRY_MS);
    }
  }, [attemptId, navigate]);

  useEffect(() => {
    aliveRef.current = true;
    bridge.hello();
    // Best effort: the title makes the receipt page nicer; submitting does not wait for it.
    studentApi.getAttempt(attemptId).then(
      (a) => {
        titleRef.current = a.course ? `${a.course} · ${a.title}` : a.title;
      },
      () => undefined,
    );
    void submit();
    return () => {
      aliveRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [attemptId, submit]);

  return (
    <div className="ext-page">
      <main className="ext-card">
        {error ? (
          <>
            <h1>Could not hand in the exam</h1>
            <p>{error}</p>
            <p className="ext-small">Ask your teacher for help. This computer stays locked until the exam is handed in.</p>
            <div className="ext-row">
              <button type="button" className="ext-btn primary" onClick={() => void submit()}>
                Try again
              </button>
              <Link to="/student" className="ext-btn">
                Back to home
              </Link>
            </div>
          </>
        ) : (
          <>
            <div className="ext-status" role="status">
              <span className="ext-spinner" aria-hidden="true" />
              <span>Handing in your exam…</span>
            </div>
            {retrying && (
              <p className="ext-alert" role="alert">
                Cannot reach the server yet. FairPlay keeps trying — stay on this screen.
              </p>
            )}
          </>
        )}
      </main>
    </div>
  );
}
