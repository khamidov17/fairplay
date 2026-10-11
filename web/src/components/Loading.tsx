export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="center-screen" role="status" aria-live="polite">
      <div>
        <div className="spinner" aria-hidden="true" />
        <p className="muted" style={{ marginTop: 12 }}>{label}</p>
      </div>
    </div>
  );
}
