// Compact question list for a paper assignment: the prompt students answer on paper, points,
// topics (feed the progress dashboard) and the key the AI grades against. Rubrics and worked
// solutions are added afterwards in the shared "Answer key" step.
import { TopicInput } from "../../../components/teacher/TopicInput";
import { Icon } from "../../../components/ui/Icon";
import { KIND_LABEL, moveItem, newQuestion, type DraftKind, type DraftQuestion, type QuestionErrors } from "../examDraft";

const KINDS: { kind: DraftKind; label: string }[] = [
  { kind: "essay", label: "+ Show your work" },
  { kind: "numeric", label: "+ Number answer" },
  { kind: "short", label: "+ Short answer" },
  { kind: "mcq", label: "+ Multiple choice" },
];

const LETTERS = "ABCDEFGH";

export function QuestionsEditor({
  questions,
  onChange,
  errors,
}: {
  questions: DraftQuestion[];
  onChange: (next: DraftQuestion[]) => void;
  errors: Record<string, QuestionErrors>;
}) {
  const set = (key: string, patch: Partial<DraftQuestion>) =>
    onChange(questions.map((q) => (q.key === key ? { ...q, ...patch } : q)));

  return (
    <div className="hw-questions">
      {questions.length === 0 && <p className="t-muted">Add the questions of the worksheet, in the order they appear.</p>}
      <ol className="hw-qlist">
        {questions.map((q, i) => {
          const err = errors[q.key] ?? {};
          const id = `hwq-${q.key}`;
          return (
            <li key={q.key} className={`t-q${Object.keys(err).length ? " invalid" : ""}`}>
              <div className="t-q-summary">
                <span className="t-q-num">{i + 1}</span>
                <span className="t-q-meta">{q.type === "essay" ? "Show your work" : KIND_LABEL[q.tf ? "tf" : q.type]}</span>
                <span className="t-q-tools">
                  <button type="button" className="t-icon-btn" aria-label={`Move question ${i + 1} up`} disabled={i === 0} onClick={() => onChange(moveItem(questions, i, -1))}>
                    <Icon name="up" size={16} />
                  </button>
                  <button type="button" className="t-icon-btn" aria-label={`Move question ${i + 1} down`} disabled={i === questions.length - 1} onClick={() => onChange(moveItem(questions, i, 1))}>
                    <Icon name="down" size={16} />
                  </button>
                  <button type="button" className="t-icon-btn danger" aria-label={`Remove question ${i + 1}`} onClick={() => onChange(questions.filter((x) => x.key !== q.key))}>
                    <Icon name="trash" size={16} />
                  </button>
                </span>
              </div>
              <div className="t-q-body">
                <div className="t-field">
                  <label htmlFor={`${id}-prompt`}>Question {i + 1}</label>
                  <textarea
                    id={`${id}-prompt`}
                    className="textarea"
                    rows={2}
                    value={q.prompt}
                    aria-invalid={!!err.prompt}
                    onChange={(e) => set(q.key, { prompt: e.target.value })}
                  />
                  {err.prompt && <span className="field-error">{err.prompt}</span>}
                </div>
                <div className="t-grid-2">
                  <div className="t-field">
                    <label htmlFor={`${id}-points`}>Points</label>
                    <input id={`${id}-points`} className="input" inputMode="numeric" value={q.points} aria-invalid={!!err.points} onChange={(e) => set(q.key, { points: e.target.value })} />
                    {err.points && <span className="field-error">{err.points}</span>}
                  </div>
                  <div className="t-field">
                    <label htmlFor={`${id}-topics`}>Topics</label>
                    <TopicInput id={`${id}-topics`} value={q.topics} invalid={!!err.topics} onChange={(t) => set(q.key, { topics: t })} />
                    {err.topics && <span className="field-error">{err.topics}</span>}
                  </div>
                </div>
                {q.type === "numeric" && (
                  <div className="t-grid-2">
                    <div className="t-field">
                      <label htmlFor={`${id}-ans`}>Correct number</label>
                      <input id={`${id}-ans`} className="input" inputMode="decimal" value={q.answer_number} aria-invalid={!!err.answer} onChange={(e) => set(q.key, { answer_number: e.target.value })} />
                      {err.answer && <span className="field-error">{err.answer}</span>}
                    </div>
                    <div className="t-field">
                      <label htmlFor={`${id}-tol`}>Tolerance (±)</label>
                      <input id={`${id}-tol`} className="input" inputMode="decimal" value={q.tolerance} onChange={(e) => set(q.key, { tolerance: e.target.value })} />
                    </div>
                  </div>
                )}
                {q.type === "short" && (
                  <div className="t-field">
                    <label htmlFor={`${id}-acc`}>Accepted answers (comma separated)</label>
                    <input id={`${id}-acc`} className="input" value={q.accepted} aria-invalid={!!err.accepted} onChange={(e) => set(q.key, { accepted: e.target.value })} />
                    {err.accepted && <span className="field-error">{err.accepted}</span>}
                  </div>
                )}
                {q.type === "mcq" && (
                  <fieldset className="t-field hw-options">
                    <legend className="t-label">Options — pick the correct one</legend>
                    {q.options.map((o, oi) => (
                      <div key={oi} className="t-opt-row">
                        <input
                          type="radio"
                          name={`${id}-correct`}
                          aria-label={`Option ${LETTERS[oi]} is correct`}
                          checked={q.correct_index === oi}
                          onChange={() => set(q.key, { correct_index: oi })}
                        />
                        <span className="t-opt-letter">{LETTERS[oi]}</span>
                        <input
                          className="input"
                          aria-label={`Option ${LETTERS[oi]}`}
                          value={o}
                          onChange={(e) => set(q.key, { options: q.options.map((x, xi) => (xi === oi ? e.target.value : x)) })}
                        />
                      </div>
                    ))}
                    {q.options.length < 8 && (
                      <button type="button" className="t-link-btn" onClick={() => set(q.key, { options: [...q.options, ""] })}>
                        + Option
                      </button>
                    )}
                    {(err.options || err.correct) && <span className="field-error">{err.options || err.correct}</span>}
                  </fieldset>
                )}
                {q.type === "essay" && (
                  <p className="t-muted t-small">Students show their working on paper. Add a rubric and the worked solution in “Answer key” so the AI grades every step.</p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="t-addbar">
        <span className="t-addbar-label">Add a question</span>
        {KINDS.map((k) => (
          <button key={k.kind} type="button" className="t-chip-btn" onClick={() => onChange([...questions, newQuestion(k.kind)])}>
            {k.label}
          </button>
        ))}
      </div>
    </div>
  );
}
