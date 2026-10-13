// Audit 2026-10-12 M-11: what must be fixed before the Mac locks. Shown instead of locking,
// because some of these (Remote Login, Screen Sharing) can't be fixed from inside the lock.
export function PreflightProblems({ problems, onRetry }: { problems: string[]; onRetry: () => void }) {
  return (
    <main className="stu-center">
      <div className="stu-card" style={{ gap: 14, maxWidth: 600 }} role="alert">
        <h1 style={{ margin: 0, fontSize: 22 }}>Fix this before the exam locks your Mac</h1>
        <ul style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6 }}>
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
        <p className="stu-muted" style={{ margin: 0 }}>
          Your exam time is running. Ask the invigilator if you need help.
        </p>
        <div className="stu-row">
          <button type="button" className="stu-btn primary" onClick={onRetry}>
            Check again
          </button>
        </div>
      </div>
    </main>
  );
}
