// Phase 3: the second marker's queue. Answers are shown blind: no first mark, no AI
// suggestion, no student name (only an anonymous id). Data: GET /api/quality/second-marking/queue
import { useEffect, useState } from "react";
import { qualityApi, type MarkerItem } from "../../../api/quality";
import { Loading } from "../../../components/Loading";
import { QuestionImages } from "../../../components/QuestionImages";
import { errorText, plural } from "../../../components/teacher/util";
import { QualityNav } from "./QualityPage";
import "./quality.css";

export default function SecondMarkingQueuePage() {
  const [items, setItems] = useState<MarkerItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  // answers marked on this visit stay visible (with a "Marked" badge) until reload
  const [justMarked, setJustMarked] = useState<Set<string>>(new Set());

  useEffect(() => {
    qualityApi.queue().then(
      (q) => setItems(q.items),
      (e) => setError(errorText(e)),
    );
  }, []);

  const toMark = (items ?? []).filter((i) => i.status === "to_mark");
  const shown = (items ?? []).filter((i) => showDone || i.status === "to_mark" || justMarked.has(i.id));

  return (
    <>
      <header className="t-page-head" style={{ alignItems: "center" }}>
        <div className="t-head-text">
          <h1>Second marking</h1>
          <p>
            {items
              ? `${plural(toMark.length, "answer")} to mark · you do not see the first mark or the AI suggestion`
              : "Answers other teachers asked you to mark blind."}
          </p>
        </div>
        <div className="t-head-actions">
          {items && items.length > toMark.length && (
            <label className="q-check">
              <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />
              Show marked answers
            </label>
          )}
          <QualityNav />
        </div>
      </header>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {!items && !error && <Loading label="Loading your queue…" />}
      {items && shown.length === 0 && (
        <div className="t-card">
          <div className="t-empty">
            <strong>Nothing to mark</strong>
            {items.length ? "You have marked every answer assigned to you." : "No answers are assigned to you."}
          </div>
        </div>
      )}
      <div className="q-stack">
        {shown.map((it) => (
          <MarkCard
            key={it.id}
            item={it}
            onSaved={(next) => {
              setJustMarked((cur) => new Set(cur).add(next.id));
              setItems((cur) => (cur ?? []).map((x) => (x.id === next.id ? next : x)));
            }}
          />
        ))}
      </div>
    </>
  );
}

function MarkCard({ item, onSaved }: { item: MarkerItem; onSaved: (it: MarkerItem) => void }) {
  const q = item.question;
  const [points, setPoints] = useState(item.my_mark ? String(item.my_mark.points) : "");
  const [comment, setComment] = useState(item.my_mark?.comment ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const closed = item.status === "closed";

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      onSaved(await qualityApi.mark(item.id, Number(points), comment));
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="t-card q-mark-card" aria-label={`${item.exam_title} ${item.anon_id}`}>
      <div className="q-stack">
        <span className="t-small t-muted">
          {item.exam_title}
          {item.course ? ` · ${item.course}` : ""} · {item.anon_id}
          {q ? ` · Q${q.index + 1}` : ""}
        </span>
        {q && (
          <>
            <strong>{q.prompt}</strong>
            {q.images.length > 0 && <QuestionImages images={q.images} />}
          </>
        )}
        <div className="q-answer">
          {item.answer.image ? (
            <img src={item.answer.image} alt="Scanned answer" />
          ) : (
            item.answer.text ?? <span className="t-muted">(blank)</span>
          )}
        </div>
      </div>
      <div className="q-stack">
        {q && q.rubric.length > 0 && (
          <div>
            <span className="t-label">Rubric</span>
            <ul className="t-small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {q.rubric.map((r) => (
                <li key={r.label}>
                  {r.label} · {r.points} pts
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="t-field">
          <label htmlFor={`sm-p-${item.id}`}>Your mark (out of {q?.points ?? "?"})</label>
          <input
            id={`sm-p-${item.id}`}
            className="input q-points-input"
            type="number"
            min={0}
            max={q?.points}
            step={0.5}
            value={points}
            disabled={closed}
            onChange={(e) => setPoints(e.target.value)}
          />
        </div>
        <div className="t-field">
          <label htmlFor={`sm-c-${item.id}`}>Comment (for the lead teacher)</label>
          <textarea
            id={`sm-c-${item.id}`}
            className="textarea"
            rows={2}
            value={comment}
            disabled={closed}
            onChange={(e) => setComment(e.target.value)}
          />
        </div>
        {err && (
          <div className="alert alert-error" role="alert">
            {err}
          </div>
        )}
        <div className="q-actions">
          {closed ? (
            <span className="t-pill grey">Moderated: closed</span>
          ) : (
            <button type="button" className="btn btn-primary" onClick={save} disabled={busy || points === ""}>
              {item.my_mark ? "Update mark" : "Save mark"}
            </button>
          )}
          {item.status === "marked" && <span className="t-pill green">Marked</span>}
        </div>
      </div>
    </article>
  );
}
