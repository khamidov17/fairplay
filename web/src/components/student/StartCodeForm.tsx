import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, NetworkError, studentApi } from "../../api/client";
import { CODE_LENGTH, StartCodeBoxes, emptyCode, type StartCodeBoxesHandle } from "./StartCodeBoxes";
import { PackageLockedError, WrongCodeError, startOffline, storedPackage } from "../../lib/offline";

/** Turns a start error into a clear sentence for the student. */
export function startErrorMessage(err: unknown): string {
  if (err instanceof PackageLockedError)
    return "No connection, and this exam's offline copy is still locked on this computer. It unlocks when you are online once after your teacher opens the exam. Ask your teacher.";
  if (err instanceof NetworkError) return "Cannot reach the server. Check the connection and try again.";
  if (err instanceof ApiError) {
    // audit 2026-10-12: app-only start and one computer per attempt
    if (err.status === 403 && /FairPlay app/i.test(err.detail)) return err.detail;
    // audit S-03: not in the exam's class
    if (err.status === 403 && /is for .* students|not for your class/i.test(err.detail)) return err.detail;
    if (err.status === 409 && /another computer|somewhere else/i.test(err.detail)) return err.detail;
    if (err.status === 403) return "Wrong start code. Check the code your teacher gave and try again.";
    if (err.status === 429) return err.detail || "Too many wrong codes. Wait 60 seconds, then try again.";
    if (err.status === 409 && /submitted/i.test(err.detail)) return "You have already submitted this exam.";
    if (err.status === 409) return "This exam is not open right now. Wait for your teacher to open it.";
    if (err.status === 404) return "This exam was not found.";
    return err.detail;
  }
  return "Something went wrong. Try again.";
}

/**
 * Start-code boxes (CODE_LENGTH digits) + "Start exam". On success navigates to the exam
 * (the attempt goes along in router state so the exam opens without a second request).
 */
export function StartCodeForm({
  examId,
  autoFocus,
  onConflict,
  onCancel,
  legend,
}: {
  examId: string;
  autoFocus?: boolean;
  /** 409 (not open / already submitted): let the parent reload its data. */
  onConflict?: () => void;
  onCancel?: () => void;
  legend?: string;
}) {
  const navigate = useNavigate();
  const [digits, setDigits] = useState<string[]>(emptyCode);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const boxes = useRef<StartCodeBoxesHandle>(null);
  const startBtn = useRef<HTMLButtonElement>(null);
  const code = digits.join("");
  const complete = code.length === CODE_LENGTH && /^\d+$/.test(code);
  const errId = `start-${examId}-err`;

  useEffect(() => {
    if (autoFocus) boxes.current?.focus(0);
  }, [autoFocus]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!complete) {
      setError(`The start code has ${CODE_LENGTH} digits.`);
      boxes.current?.focus(digits.indexOf("") === -1 ? 0 : digits.indexOf(""));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      let attempt;
      try {
        attempt = await studentApi.start(examId, code);
      } catch (err) {
        // Slice 6: no network at start -> open the package downloaded at check-in.
        if (!(err instanceof NetworkError) || !storedPackage(examId)) throw err;
        try {
          attempt = await startOffline(examId, code);
        } catch (offErr) {
          if (offErr instanceof PackageLockedError) throw offErr;
          throw offErr instanceof WrongCodeError ? new ApiError(403, "Wrong start code") : err;
        }
      }
      navigate(`/student/exam/${encodeURIComponent(attempt.attempt_id)}`, { state: { attempt } });
    } catch (err) {
      setBusy(false);
      setError(startErrorMessage(err));
      if (err instanceof ApiError && err.status === 409) {
        onConflict?.();
        return;
      }
      setDigits(emptyCode());
      setTimeout(() => boxes.current?.focus(0), 0);
    }
  }

  return (
    <form onSubmit={submit} className="stu-code-panel" noValidate>
      <StartCodeBoxes
        ref={boxes}
        idPrefix={`code-${examId}`}
        value={digits}
        onChange={(d) => {
          setDigits(d);
          if (error) setError(null);
        }}
        onComplete={() => startBtn.current?.focus()}
        invalid={!!error}
        describedBy={error ? errId : undefined}
        disabled={busy}
        legend={legend}
      />
      {error && (
        <div id={errId} className="stu-alert error" role="alert">
          {error}
        </div>
      )}
      <div className="stu-row">
        <button ref={startBtn} type="submit" className="stu-btn primary lg" style={{ flex: "1 1 160px" }} disabled={busy}>
          {busy ? "Checking…" : "Start exam"}
        </button>
        {onCancel && (
          <button type="button" className="stu-btn lg" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
