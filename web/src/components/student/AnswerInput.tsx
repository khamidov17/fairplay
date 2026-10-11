import { useState } from "react";
import type { AnswerValue, StudentQuestion } from "../../api/types";
import { LETTERS, fmtNum, wordCount } from "./format";
import { ChevronRightIcon } from "./icons";

interface Props {
  question: StudentQuestion;
  value: AnswerValue | undefined;
  onChange: (v: AnswerValue | undefined) => void;
  /** id of the element that labels the question (the prompt heading). */
  labelledBy: string;
}

/** The answer control for one question. Remount (key) per question. */
export function AnswerInput({ question, value, onChange, labelledBy }: Props) {
  if (question.type === "mcq") return <McqInput question={question} value={value} onChange={onChange} labelledBy={labelledBy} />;
  if (question.type === "numeric") return <NumericInput question={question} value={value} onChange={onChange} />;
  if (question.type === "essay") return <EssayInput question={question} value={value} onChange={onChange} labelledBy={labelledBy} />;
  return <ShortInput question={question} value={value} onChange={onChange} />;
}

function McqInput({ question, value, onChange, labelledBy }: Props) {
  const count = question.options.length;
  // Roving tabindex: the selected option (or the first) is the tab stop; arrows move + select.
  const focusIndex = typeof value === "number" && value >= 0 && value < count ? value : 0;
  const move = (from: number, delta: number, el: HTMLElement) => {
    const next = (from + delta + count) % count;
    onChange(next);
    const group = el.closest("[role=radiogroup]");
    (group?.querySelectorAll<HTMLElement>("[role=radio]")[next])?.focus();
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="stu-options" role="radiogroup" aria-labelledby={labelledBy}>
        {question.options.map((opt, i) => {
          const selected = value === i;
          return (
            <button
              key={i}
              type="button"
              role="radio"
              aria-checked={selected}
              tabIndex={i === focusIndex ? 0 : -1}
              className="stu-option"
              onClick={() => onChange(selected ? undefined : i)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown" || e.key === "ArrowRight") {
                  e.preventDefault();
                  move(i, 1, e.currentTarget);
                } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
                  e.preventDefault();
                  move(i, -1, e.currentTarget);
                }
              }}
            >
              <span className="stu-option-letter" aria-hidden="true">
                {LETTERS[i]}
              </span>
              <span className="stu-option-text">{opt}</span>
            </button>
          );
        })}
      </div>
      <p className="stu-hint">Click an option to choose it. Click it again to clear.</p>
    </div>
  );
}

function NumericInput({ question, value, onChange }: Omit<Props, "labelledBy">) {
  const [text, setText] = useState(value === undefined ? "" : String(value));
  const norm = text.trim().replace(",", ".");
  const bad = norm !== "" && !Number.isFinite(Number(norm));
  const id = `ans-${question.id}`;
  return (
    <div className="stu-field">
      <label htmlFor={id}>Your answer (a number)</label>
      <input
        id={id}
        className="stu-answer-input num"
        type="text"
        inputMode="decimal"
        autoComplete="off"
        spellCheck={false}
        value={text}
        aria-invalid={bad || undefined}
        aria-describedby={bad ? `${id}-err` : undefined}
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          const n = t.trim().replace(",", ".");
          if (n === "") onChange(undefined);
          else if (Number.isFinite(Number(n))) onChange(Number(n));
        }}
      />
      {bad && (
        <span id={`${id}-err`} className="stu-field-error">
          Please type a number, for example 3.14
        </span>
      )}
    </div>
  );
}

function ShortInput({ question, value, onChange }: Omit<Props, "labelledBy">) {
  const [text, setText] = useState(value === undefined ? "" : String(value));
  const id = `ans-${question.id}`;
  return (
    <div className="stu-field">
      <label htmlFor={id}>Your answer</label>
      <input
        id={id}
        className="stu-answer-input"
        type="text"
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        maxLength={10000}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          onChange(e.target.value.trim() === "" ? undefined : e.target.value);
        }}
      />
    </div>
  );
}

/** "aim for 600–900 words" / "at least 600 words" / "at most 900 words" / "" */
export function wordAim(min: number | null | undefined, max: number | null | undefined): string {
  if (min && max) return `aim for ${min}–${max} words`;
  if (min) return `aim for at least ${min} words`;
  if (max) return `at most ${max} words`;
  return "";
}

/** The essay rubric, shown as a quiet collapsible under the prompt. */
export function RubricPanel({ question }: { question: StudentQuestion }) {
  const rubric = question.rubric ?? [];
  if (rubric.length === 0) return null;
  const total = rubric.reduce((s, r) => s + (Number(r.points) || 0), 0);
  return (
    <details className="stu-rubric-details">
      <summary>
        <ChevronRightIcon />
        How this is graded
        <span className="pts">
          {rubric.length === 1 ? "1 criterion" : `${rubric.length} criteria`} · {fmtNum(total)} {total === 1 ? "point" : "points"}
        </span>
      </summary>
      <ul className="stu-rubric-list">
        {rubric.map((r, i) => (
          <li key={i}>
            <span>{r.label}</span>
            <b>
              {fmtNum(r.points)} <span className="sr-only">points</span>
            </b>
          </li>
        ))}
      </ul>
    </details>
  );
}

export function EssayInput({ question, value, onChange, labelledBy }: Props) {
  // labelledBy (the prompt) describes the textarea; its label is the visible "Your answer".
  const [text, setText] = useState(typeof value === "string" ? value : "");
  const words = wordCount(text);
  const min = question.min_words ?? null;
  const max = question.max_words ?? null;
  const aim = wordAim(min, max);
  const target = max ?? min;
  const over = max !== null && words > max;
  const pct = target ? Math.min(100, Math.round((words / target) * 100)) : 0;
  const id = `ans-${question.id}`;
  return (
    <div className="stu-editor">
      <div className="stu-editor-bar">
        <label htmlFor={id}>Your answer</label>
        <span className="stu-spacer" />
        <span>Spell check off</span>
      </div>
      <textarea
        id={id}
        aria-describedby={`${labelledBy} ${id}-count`}
        value={text}
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        autoComplete="off"
        maxLength={50000}
        rows={14}
        onChange={(e) => {
          setText(e.target.value);
          onChange(e.target.value.trim() === "" ? undefined : e.target.value);
        }}
      />
      <div className="stu-editor-foot">
        <div className="stu-wordcount" id={`${id}-count`}>
          <span>
            <span data-testid="word-count">{words === 1 ? "1 word" : `${words} words`}</span>
            {aim && <span className="aim"> · {aim}</span>}
            {over && <span className="over"> · over the limit</span>}
          </span>
          {target ? (
            <div className={`stu-progress${over ? " over" : ""}`} aria-hidden="true">
              <span style={{ width: `${pct}%` }} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
