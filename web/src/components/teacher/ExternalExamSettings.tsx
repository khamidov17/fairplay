// Slice 3: "Where do students answer?" — questions in FairPlay, or an exam held on another
// website (Canvas quiz, Google Form, Moodle…) that the Mac app opens inside the lockdown.
// Self-contained: the parent keeps the value and sends externalExamInput(value) on save.
import { useEffect, useId, useState } from "react";
import type { Delivery, ExamMode } from "../../api/types";
import {
  normalizeDomains,
  splitDomainText,
  urlHost,
  type ExternalSettingsErrors,
  type ExternalSettingsValue,
} from "./externalValidation";
import "../../styles/external.css";
import "../../styles/slice6.css";

export interface ExternalExamSettingsProps {
  value: ExternalSettingsValue;
  onChange: (next: ExternalSettingsValue) => void;
  /** Errors to show (from validateExternalSettings or the server). */
  errors?: ExternalSettingsErrors;
  disabled?: boolean;
  /**
   * Slice 6: when given, three delivery cards replace the two mode cards. Picking "website"
   * sets mode "external"; "computer" / "paper" set mode "questions".
   */
  delivery?: Delivery;
  onDeliveryChange?: (next: Delivery) => void;
}

const DELIVERY_CHOICES: { delivery: Delivery; title: string; hint: string }[] = [
  {
    delivery: "computer",
    title: "On computers (FairPlay app, locked)",
    hint: "Students answer your questions in the locked FairPlay app. Automatic questions are graded for you.",
  },
  {
    delivery: "paper",
    title: "On paper (print, scan, AI-assisted grading)",
    hint: "Print one booklet per student, scan the answers, and check AI-suggested grades before they count.",
  },
  {
    delivery: "website",
    title: "On another website",
    hint: "Students answer on Canvas, Google Forms, Moodle or another site, opened inside the locked FairPlay app.",
  },
];

const CHOICES: { mode: ExamMode; title: string; hint: string }[] = [
  {
    mode: "questions",
    title: "Questions in FairPlay",
    hint: "Write the questions here. FairPlay grades the automatic ones for you.",
  },
  {
    mode: "external",
    title: "On another website",
    hint: "Students answer on Canvas, Google Forms, Moodle or another site, opened inside the locked FairPlay app.",
  },
];

export function ExternalExamSettings({
  value,
  onChange,
  errors = {},
  disabled,
  delivery,
  onDeliveryChange,
}: ExternalExamSettingsProps) {
  const uid = useId();
  const name = `exam-mode-${uid}`;
  // The textarea keeps its own text so typing a newline or a half-written name is not
  // rewritten under the cursor; the parent gets the list of non-empty lines.
  const [domainText, setDomainText] = useState(() => value.allowed_domains.join("\n"));
  useEffect(() => {
    const mine = splitDomainText(domainText);
    const same =
      mine.length === value.allowed_domains.length && mine.every((d, i) => d === value.allowed_domains[i]);
    if (!same) setDomainText(value.allowed_domains.join("\n"));
    // only react to changes coming from the parent
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.allowed_domains]);

  const set = (patch: Partial<ExternalSettingsValue>) => onChange({ ...value, ...patch });
  const external = value.mode === "external";
  const host = urlHost(value.external_url);
  const preview = normalizeDomains([...(host ? [host] : []), ...value.allowed_domains]);

  return (
    <fieldset className="ext-settings" disabled={disabled}>
      <legend>Where do students answer?</legend>
      {delivery !== undefined && onDeliveryChange ? (
        <div className="ext-choices three">
          {DELIVERY_CHOICES.map((c) => {
            const current: Delivery = value.mode === "external" ? "website" : delivery === "paper" ? "paper" : "computer";
            const selected = current === c.delivery;
            return (
              <label key={c.delivery} className={`ext-choice${selected ? " selected" : ""}`}>
                <input
                  type="radio"
                  name={name}
                  value={c.delivery}
                  checked={selected}
                  onChange={() => {
                    const mode: ExamMode = c.delivery === "website" ? "external" : "questions";
                    if (mode !== value.mode) set({ mode });
                    onDeliveryChange(c.delivery);
                  }}
                />
                <span>
                  <span className="ext-choice-title">{c.title}</span>
                  <span className="ext-choice-hint">{c.hint}</span>
                </span>
              </label>
            );
          })}
        </div>
      ) : (
      <div className="ext-choices">
        {CHOICES.map((c) => {
          const selected = value.mode === c.mode;
          return (
            <label key={c.mode} className={`ext-choice${selected ? " selected" : ""}`}>
              <input
                type="radio"
                name={name}
                value={c.mode}
                checked={selected}
                onChange={() => set({ mode: c.mode })}
              />
              <span>
                <span className="ext-choice-title">{c.title}</span>
                <span className="ext-choice-hint">{c.hint}</span>
              </span>
            </label>
          );
        })}
      </div>
      )}

      {external && (
        <div className="ext-fields">
          <div className="ext-field">
            <label htmlFor="exam-external-url">Exam website address</label>
            <input
              id="exam-external-url"
              className="ext-input"
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              placeholder="https://canvas.dku.edu/courses/123/quizzes/456"
              value={value.external_url ?? ""}
              aria-invalid={errors.external_url ? true : undefined}
              aria-describedby="exam-external-url-hint"
              onChange={(e) => set({ external_url: e.target.value })}
            />
            <p id="exam-external-url-hint" className="ext-hint">
              The page students should see first, for example the quiz or form link. Students open it from the
              FairPlay app after entering the start code.
            </p>
            {errors.external_url && (
              <p className="ext-error" role="alert">
                {errors.external_url}
              </p>
            )}
          </div>

          <div className="ext-field">
            <label htmlFor="exam-allowed-domains">
              Other websites students may open <span className="ext-optional">(one per line)</span>
            </label>
            <textarea
              id="exam-allowed-domains"
              className="ext-input"
              rows={4}
              spellCheck={false}
              autoCapitalize="off"
              placeholder={"login.microsoftonline.com\ndku.edu"}
              value={domainText}
              aria-invalid={errors.allowed_domains ? true : undefined}
              aria-describedby="exam-allowed-domains-hint"
              onChange={(e) => {
                setDomainText(e.target.value);
                set({ allowed_domains: splitDomainText(e.target.value) });
              }}
            />
            <p id="exam-allowed-domains-hint" className="ext-hint">
              Add every website the exam needs, including the sign-in page (e.g. login.microsoftonline.com). A
              name also allows its sub-sites: dku.edu allows canvas.dku.edu. Everything else is blocked and shown
              to you in the live room.
            </p>
            {errors.allowed_domains && (
              <p className="ext-error" role="alert">
                {errors.allowed_domains}
              </p>
            )}
            {preview.length > 0 && (
              <>
                <p className="ext-hint" id="exam-allowed-preview">
                  Students can open:
                </p>
                <ul className="ext-chips" aria-labelledby="exam-allowed-preview">
                  {preview.map((d) => (
                    <li key={d}>{d}</li>
                  ))}
                </ul>
              </>
            )}
          </div>

          <div className="ext-field">
            <label htmlFor="exam-finish-contains">
              Finish automatically when the page address contains <span className="ext-optional">(optional)</span>
            </label>
            <input
              id="exam-finish-contains"
              className="ext-input"
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="formResponse"
              value={value.finish_url_contains ?? ""}
              aria-invalid={errors.finish_url_contains ? true : undefined}
              aria-describedby="exam-finish-contains-hint"
              onChange={(e) => set({ finish_url_contains: e.target.value })}
            />
            <p id="exam-finish-contains-hint" className="ext-hint">
              When the website shows its “submitted” page, FairPlay hands the exam in and unlocks the computer.
              For Google Forms use formResponse. Leave empty if students press “Finish exam” themselves.
            </p>
            {errors.finish_url_contains && (
              <p className="ext-error" role="alert">
                {errors.finish_url_contains}
              </p>
            )}
          </div>
        </div>
      )}
    </fieldset>
  );
}

export default ExternalExamSettings;
