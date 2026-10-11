// Slice 6 C: topic tags for a question. Chips + a text box: Enter or comma adds the typed
// topic, Backspace in an empty box removes the last chip, arrows pick from suggestions
// (GET /api/topics, the teacher's earlier topics, most used first).
import { useId, useMemo, useState, type KeyboardEvent } from "react";
import type { TopicCount } from "../../api/types";
import "../../styles/slice6.css";

export const MAX_TOPICS = 5;
export const MAX_TOPIC_CHARS = 60;

/** Same rules as the server: trimmed, single spaces, lower-case. */
export function normalizeTopic(raw: string): string {
  return raw.trim().split(/\s+/).join(" ").toLowerCase();
}

/** Add one topic to a list (dedupe, limits). Returns the same list when nothing changes. */
export function addTopic(list: string[], raw: string): string[] {
  const t = normalizeTopic(raw).slice(0, MAX_TOPIC_CHARS);
  if (!t || list.includes(t) || list.length >= MAX_TOPICS) return list;
  return [...list, t];
}

export function TopicInput({
  id,
  value,
  onChange,
  suggestions = [],
  invalid,
  describedBy,
}: {
  /** id of the text input (ends with "-topics" in the exam editor) */
  id: string;
  value: string[];
  onChange: (next: string[]) => void;
  suggestions?: TopicCount[];
  invalid?: boolean;
  describedBy?: string;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const listId = `${useId()}-topic-list`;
  const full = value.length >= MAX_TOPICS;

  const matches = useMemo(() => {
    const q = normalizeTopic(text);
    if (!q) return [];
    return suggestions
      .filter((s) => s.topic.includes(q) && !value.includes(s.topic))
      .slice(0, 8);
  }, [text, suggestions, value]);
  const showMenu = open && matches.length > 0;

  const commit = (raw: string) => {
    const next = addTopic(value, raw);
    if (next !== value) onChange(next);
    setText("");
    setActive(-1);
  };

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault(); // never submit the exam form from here
      if (showMenu && active >= 0 && matches[active]) commit(matches[active].topic);
      else if (text.trim()) commit(text);
    } else if (e.key === "Backspace" && text === "" && value.length > 0) {
      e.preventDefault();
      onChange(value.slice(0, -1));
    } else if (e.key === "ArrowDown" && matches.length) {
      e.preventDefault();
      setOpen(true);
      setActive((i) => (i + 1) % matches.length);
    } else if (e.key === "ArrowUp" && matches.length) {
      e.preventDefault();
      setOpen(true);
      setActive((i) => (i <= 0 ? matches.length - 1 : i - 1));
    } else if (e.key === "Escape" && showMenu) {
      e.preventDefault();
      setOpen(false);
    }
  }

  return (
    <div className={`fp-topic-box${invalid ? " invalid" : ""}`}>
      {value.map((t) => (
        <span key={t} className="fp-topic-chip">
          {t}
          <button type="button" aria-label={`Remove topic ${t}`} onClick={() => onChange(value.filter((x) => x !== t))}>
            ×
          </button>
        </span>
      ))}
      <input
        id={id}
        type="text"
        value={text}
        maxLength={MAX_TOPIC_CHARS + 5}
        disabled={full}
        placeholder={full ? "5 topics (the most)" : value.length ? "Add another" : "e.g. fractions"}
        autoComplete="off"
        role="combobox"
        aria-expanded={showMenu}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showMenu && active >= 0 ? `${listId}-${active}` : undefined}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        onChange={(e) => {
          const v = e.target.value;
          if (v.includes(",")) {
            // pasted "a, b, c": add every finished part
            const parts = v.split(",");
            let next = value;
            for (const p of parts.slice(0, -1)) next = addTopic(next, p);
            if (next !== value) onChange(next);
            setText(parts[parts.length - 1]);
          } else setText(v);
          setOpen(true);
          setActive(-1);
        }}
        onKeyDown={onKey}
        onBlur={() => {
          setOpen(false);
          if (text.trim()) commit(text);
        }}
      />
      {showMenu && (
        <ul className="fp-topic-menu" id={listId} role="listbox" aria-label="Topics you used before">
          {matches.map((m, i) => (
            <li
              key={m.topic}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => {
                e.preventDefault(); // keep focus in the input
                commit(m.topic);
              }}
            >
              {m.topic}
              <span>
                {m.question_count} {m.question_count === 1 ? "question" : "questions"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
