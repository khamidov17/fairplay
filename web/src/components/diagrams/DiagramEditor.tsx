import { useEffect, useRef, useState } from "react";
import { slice6Api } from "../../api/client";
import { diagramsApi, type DiagramFeature, type DiagramKindInfo, type DiagramSpec } from "../../api/diagrams";
import {
  checklistError,
  checklistTotal,
  defaultSpec,
  formatPointList,
  parsePointList,
  rescaleFeatures,
} from "./diagramLogic";
import "../../styles/diagrams.css";

const POINT_KEYS = ["points", "key_points"];
let added = 0;

/**
 * "Graph / chart / diagram" settings of one written-answer question: the subtype, its feature
 * checklist (defaults filled in, points sized to the question) and an optional reference drawing.
 * Used by the exam editor and by the Scan & Grade answer-key step. `value === null` = a normal
 * written answer.
 */
export function DiagramEditor({
  idPrefix,
  value,
  points,
  onChange,
  offLabel = "Written answer (text, working)",
}: {
  idPrefix: string;
  value: DiagramSpec | null;
  points: number;
  onChange: (spec: DiagramSpec | null) => void;
  offLabel?: string;
}) {
  const [kinds, setKinds] = useState<DiagramKindInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const id = (s: string) => `${idPrefix}-dg-${s}`;

  useEffect(() => {
    let alive = true;
    diagramsApi.kinds().then(
      (k) => alive && setKinds(k),
      () => alive && setLoadError("Could not load the checklist templates."),
    );
    return () => {
      alive = false;
    };
  }, []);

  const groups = new Map<string, DiagramKindInfo[]>();
  for (const k of kinds ?? []) groups.set(k.group, [...(groups.get(k.group) ?? []), k]);
  const kindInfo = kinds?.find((k) => k.kind === value?.kind);
  const total = value ? checklistTotal(value.features) : 0;
  const err = value ? checklistError(value, points) : null;

  function pick(kind: string) {
    if (!kind) return onChange(null);
    const info = kinds?.find((k) => k.kind === kind);
    if (!info) return;
    onChange({ ...defaultSpec(info, points), reference_image: value?.reference_image ?? null, notes: value?.notes ?? "" });
  }
  const setFeature = (i: number, patch: Partial<DiagramFeature>) =>
    value && onChange({ ...value, features: value.features.map((f, j) => (j === i ? { ...f, ...patch } : f)) });

  async function upload(file: File) {
    if (!value) return;
    setUploading(true);
    setUploadError(null);
    try {
      const up = await slice6Api.uploadImage(file, file.name);
      onChange({ ...value, reference_image: up.url });
    } catch {
      setUploadError("Upload failed — use a PNG or JPEG under 5 MB.");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <fieldset className="dg-editor">
      <legend className="t-label">Answer is</legend>
      <div className="t-field">
        <label htmlFor={id("kind")} className="sr-only">
          Answer kind
        </label>
        <select id={id("kind")} className="input" value={value?.kind ?? ""} onChange={(e) => pick(e.target.value)}>
          <option value="">{offLabel}</option>
          {value && !kinds && <option value={value.kind}>{value.kind}</option>}
          {[...groups.entries()].map(([g, ks]) => (
            <optgroup key={g} label={`Graph / chart / diagram — ${g}`}>
              {ks.map((k) => (
                <option key={k.kind} value={k.kind}>
                  {k.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {loadError && <span className="field-error">{loadError}</span>}
        {kindInfo && <span className="hint">{kindInfo.hint} The AI checks each feature on the drawing.</span>}
      </div>

      {value && (
        <>
          <div className="dg-ed-head">
            <span className="t-label">Feature checklist</span>
            <span className={`ak-total${err ? " is-bad" : ""}`} aria-live="polite">
              {total} / {points} pts
            </span>
          </div>
          <ol className="dg-ed-list">
            {value.features.map((f, i) => {
              const xy = POINT_KEYS.includes(f.key) || (f.points_xy?.length ?? 0) > 0;
              return (
                <li key={f.id} className="dg-ed-row">
                  <input
                    className="input"
                    aria-label={`Feature ${i + 1}`}
                    value={f.label}
                    onChange={(e) => setFeature(i, { label: e.target.value })}
                  />
                  <input
                    className="input mono dg-ed-pts"
                    aria-label={`Feature ${i + 1} points`}
                    type="number"
                    min={0}
                    step={0.5}
                    value={Number.isFinite(f.points) ? f.points : ""}
                    onChange={(e) => setFeature(i, { points: e.target.value === "" ? NaN : Number(e.target.value) })}
                  />
                  <button
                    type="button"
                    className="t-icon-btn"
                    aria-label={`Remove feature ${i + 1}`}
                    onClick={() => onChange({ ...value, features: value.features.filter((_, j) => j !== i) })}
                  >
                    ×
                  </button>
                  <input
                    className="input dg-ed-exp"
                    aria-label={`Feature ${i + 1} expected`}
                    placeholder="Expected (e.g. x-intercepts −1 and 3)"
                    value={f.expected}
                    onChange={(e) => setFeature(i, { expected: e.target.value })}
                  />
                  {xy && (
                    <PointsField
                      label={`Feature ${i + 1} points to plot`}
                      value={f.points_xy ?? []}
                      tolerance={f.tolerance ?? 0.5}
                      onChange={(points_xy, tolerance) => setFeature(i, { points_xy, tolerance })}
                    />
                  )}
                </li>
              );
            })}
          </ol>
          <div className="sc-inline">
            <button
              type="button"
              className="t-link-btn"
              onClick={() =>
                onChange({
                  ...value,
                  features: [
                    ...value.features,
                    { id: `f${Date.now().toString(36).slice(-6)}${++added}`, key: "custom", label: "", points: 0, expected: "" },
                  ],
                })
              }
            >
              + Add feature
            </button>
            {kindInfo && (
              <button type="button" className="t-link-btn" onClick={() => pick(value.kind)}>
                Reset to defaults
              </button>
            )}
            {err && Math.abs(total - points) > 1e-6 && value.features.length > 0 && (
              <button
                type="button"
                className="t-link-btn"
                onClick={() => onChange({ ...value, features: rescaleFeatures(value.features, points) })}
              >
                Fit to {points} pts
              </button>
            )}
          </div>
          {err && <span className="field-error">{err}</span>}

          <div className="t-field">
            <label htmlFor={id("notes")}>Notes for the AI (optional)</label>
            <textarea
              id={id("notes")}
              className="textarea"
              rows={2}
              placeholder="e.g. f(x) = (x − 1)(x + 3) / (x − 2); students may use any sensible scale."
              value={value.notes}
              onChange={(e) => onChange({ ...value, notes: e.target.value })}
            />
          </div>
          <div className="dg-ref">
            <span className="t-label">Reference drawing (optional)</span>
            {value.reference_image ? (
              <div className="dg-ref-row">
                <img src={value.reference_image} alt="Reference drawing" />
                <button type="button" className="t-btn-outline" onClick={() => onChange({ ...value, reference_image: null })}>
                  Remove reference
                </button>
              </div>
            ) : (
              <label className="t-btn-outline sc-file-btn">
                {uploading ? "Uploading…" : "Upload your correct drawing"}
                <input
                  ref={fileRef}
                  type="file"
                  className="sr-only"
                  accept="image/png,image/jpeg"
                  aria-label="Upload reference drawing"
                  disabled={uploading}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) void upload(file);
                  }}
                />
              </label>
            )}
            {uploadError && <span className="field-error">{uploadError}</span>}
          </div>
        </>
      )}
    </fieldset>
  );
}

function PointsField({
  label,
  value,
  tolerance,
  onChange,
}: {
  label: string;
  value: [number, number][];
  tolerance: number;
  onChange: (xy: [number, number][], tolerance: number) => void;
}) {
  const [text, setText] = useState(formatPointList(value));
  const [bad, setBad] = useState(false);
  return (
    <div className="dg-ed-xy">
      <input
        className="input mono"
        aria-label={label}
        placeholder="x, y; x, y (e.g. 1, 2; 2, 4.5)"
        value={text}
        aria-invalid={bad}
        onChange={(e) => {
          setText(e.target.value);
          const xy = parsePointList(e.target.value);
          setBad(xy === null);
          if (xy) onChange(xy, tolerance);
        }}
      />
      <input
        className="input mono dg-ed-pts"
        aria-label={`${label}: tolerance`}
        type="number"
        min={0}
        step={0.1}
        value={tolerance}
        onChange={(e) => onChange(value, Math.max(0, Number(e.target.value) || 0))}
      />
      {bad && <span className="field-error">Write points as “x, y; x, y”.</span>}
    </div>
  );
}

