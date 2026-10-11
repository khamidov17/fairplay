import { forwardRef, useImperativeHandle, useRef, type ClipboardEvent, type KeyboardEvent } from "react";

export const CODE_LENGTH = 4;

export interface StartCodeBoxesHandle {
  focus(index?: number): void;
}

interface Props {
  /** Exactly CODE_LENGTH entries, each "" or one digit. */
  value: string[];
  onChange(next: string[]): void;
  /** Called when the last empty box gets a digit (all boxes filled). */
  onComplete?(code: string): void;
  idPrefix: string;
  legend?: string;
  invalid?: boolean;
  describedBy?: string;
  disabled?: boolean;
}

export const emptyCode = (): string[] => Array.from({ length: CODE_LENGTH }, () => "");

/**
 * Start code as four single-digit boxes: typing a digit moves to the next box,
 * Backspace clears and moves back, pasting a code fills all boxes.
 */
export const StartCodeBoxes = forwardRef<StartCodeBoxesHandle, Props>(function StartCodeBoxes(
  { value, onChange, onComplete, idPrefix, legend = "Start code — your teacher reads it out", invalid, describedBy, disabled },
  ref,
) {
  const inputs = useRef<(HTMLInputElement | null)[]>([]);

  const focusAt = (i: number) => {
    const el = inputs.current[Math.max(0, Math.min(CODE_LENGTH - 1, i))];
    if (el) {
      el.focus();
      el.select();
    }
  };

  useImperativeHandle(ref, () => ({ focus: (i = 0) => focusAt(i) }));

  /** Write digits starting at box `from`; returns the new array. */
  const fill = (from: number, digits: string) => {
    const next = [...value];
    let i = from;
    for (const d of digits) {
      if (i >= CODE_LENGTH) break;
      next[i] = d;
      i += 1;
    }
    onChange(next);
    const anyEmpty = next.indexOf("");
    if (anyEmpty === -1) {
      onComplete?.(next.join("")); // the parent may move focus to its Start button
    } else {
      const nextEmpty = next.findIndex((d, idx) => idx >= i && d === "");
      focusAt(nextEmpty !== -1 ? nextEmpty : anyEmpty);
    }
  };

  const onKeyDown = (i: number, e: KeyboardEvent<HTMLInputElement>) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return; // keep paste / shortcuts
    if (/^\d$/.test(e.key)) {
      e.preventDefault();
      fill(i, e.key);
      return;
    }
    if (e.key === "Backspace") {
      e.preventDefault();
      const next = [...value];
      if (next[i] !== "") {
        next[i] = "";
        onChange(next);
      } else if (i > 0) {
        next[i - 1] = "";
        onChange(next);
        focusAt(i - 1);
      }
      return;
    }
    if (e.key === "Delete") {
      e.preventDefault();
      const next = [...value];
      next[i] = "";
      onChange(next);
      return;
    }
    if (e.key === "ArrowLeft" && i > 0) {
      e.preventDefault();
      focusAt(i - 1);
    } else if (e.key === "ArrowRight" && i < CODE_LENGTH - 1) {
      e.preventDefault();
      focusAt(i + 1);
    }
  };

  const onPaste = (i: number, e: ClipboardEvent<HTMLInputElement>) => {
    const digits = e.clipboardData.getData("text").replace(/\D/g, "");
    e.preventDefault();
    if (!digits) return;
    // A whole code always fills from the first box.
    fill(digits.length >= CODE_LENGTH ? 0 : i, digits.slice(0, CODE_LENGTH));
  };

  return (
    <fieldset className="stu-code" aria-describedby={describedBy}>
      <legend>{legend}</legend>
      <div className="stu-code-boxes">
        {value.map((d, i) => (
          <input
            key={i}
            ref={(el) => {
              inputs.current[i] = el;
            }}
            id={`${idPrefix}-${i}`}
            className="stu-code-box"
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete={i === 0 ? "one-time-code" : "off"}
            aria-label={`Digit ${i + 1} of ${CODE_LENGTH}`}
            aria-invalid={invalid || undefined}
            disabled={disabled}
            value={d}
            onFocus={(e) => e.target.select()}
            onKeyDown={(e) => onKeyDown(i, e)}
            onPaste={(e) => onPaste(i, e)}
            onChange={(e) => {
              // Reached by input methods that skip keydown (autofill, mobile keyboards, fill()).
              const digits = e.target.value.replace(/\D/g, "");
              if (digits === "") {
                const next = [...value];
                next[i] = "";
                onChange(next);
              } else if (digits.length > 1 && value[i] !== "" && digits.length === 2) {
                // typed into a filled box: keep the new digit
                fill(i, digits[0] === value[i] ? digits[1] : digits[0]);
              } else {
                fill(i, digits);
              }
            }}
          />
        ))}
      </div>
    </fieldset>
  );
});
