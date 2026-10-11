import type { SyncStatus } from "../../lib/answerQueue";
import { formatCountdown } from "../../lib/timer";
import { ClockIcon, CloudCheckIcon, DiskIcon, LockIcon, UnlockIcon } from "./icons";

/**
 * Slim white top bar shown while taking an exam:
 * lock state · course + title · save status · timer (amber under 5 minutes).
 * In a plain browser the yellow "not locked" strip above this bar carries the lock state.
 */
export function LockedBar({
  title,
  course,
  locked,
  native,
  sync,
  remaining,
}: {
  title: string;
  course?: string;
  locked: boolean;
  /** Running in the Mac app (when false, the yellow strip explains the lock state). */
  native?: boolean;
  sync: SyncStatus;
  remaining: number;
}) {
  const lowTime = remaining < 5 * 60 * 1000;
  const synced = sync === "synced";
  return (
    <header className="stu-lockbar">
      {locked ? (
        <span className="stu-lock-badge">
          <LockIcon />
          Locked
        </span>
      ) : native ? (
        <span className="stu-lock-badge off">
          <UnlockIcon />
          Not locked
        </span>
      ) : null}
      <span className="stu-lockbar-title">
        {course && <small>{course}</small>}
        <span className="t">{title}</span>
      </span>
      <span className={`stu-sync ${synced ? "synced" : "pending"}`} role="status" aria-live="polite">
        {synced ? <CloudCheckIcon /> : <DiskIcon />}
        {synced ? "All answers saved" : "Saved on this computer"}
      </span>
      <span className={`stu-timer${lowTime ? " low" : ""}`} role="timer" aria-label="Time left">
        <ClockIcon />
        {Number.isFinite(remaining) ? formatCountdown(remaining) : "--:--"}
      </span>
    </header>
  );
}
