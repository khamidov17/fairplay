// Slice 6 B: add / remove pictures for one question (max 6), each with a short description
// (alt text) read aloud by screen readers. Drag pictures onto the box or pick files.
import { useRef, useState, type DragEvent } from "react";
import { ApiError, NetworkError, slice6Api } from "../../api/client";
import type { QuestionImage } from "../../api/types";
import "../../styles/slice6.css";

export const MAX_IMAGES = 6;
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

/** Reason a file cannot be used, or null. SVG is refused (it can carry scripts). */
export function imageFileProblem(file: { name: string; type: string; size: number }): string | null {
  if (!IMAGE_TYPES.includes(file.type)) return `${file.name}: use a PNG, JPEG, GIF or WebP picture.`;
  if (file.size > MAX_IMAGE_BYTES) return `${file.name}: pictures can be at most 5 MB.`;
  return null;
}

export function QuestionImagesEditor({
  idPrefix,
  images,
  onChange,
}: {
  idPrefix: string;
  images: QuestionImage[];
  onChange: (next: QuestionImage[]) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // uploads finish one by one: keep the latest list in a ref so none is lost
  const latest = useRef(images);
  latest.current = images;

  async function addFiles(files: File[]) {
    setError(null);
    const room = MAX_IMAGES - latest.current.length - busy;
    if (room <= 0) {
      setError(`A question can have at most ${MAX_IMAGES} pictures.`);
      return;
    }
    const problems: string[] = [];
    const ok = files.filter((f) => {
      const p = imageFileProblem(f);
      if (p) problems.push(p);
      return !p;
    });
    if (ok.length > room) problems.push(`Only ${room} more ${room === 1 ? "picture fits" : "pictures fit"}.`);
    const todo = ok.slice(0, room);
    setBusy((n) => n + todo.length);
    for (const f of todo) {
      try {
        const up = await slice6Api.uploadImage(f, f.name);
        const next = [...latest.current, { url: up.url, alt: "" }];
        latest.current = next;
        onChange(next);
      } catch (e) {
        problems.push(
          e instanceof ApiError
            ? `${f.name}: ${e.detail}`
            : e instanceof NetworkError
              ? `${f.name}: cannot reach the server.`
              : `${f.name}: upload failed.`,
        );
      } finally {
        setBusy((n) => n - 1);
      }
    }
    if (problems.length) setError(problems.join(" "));
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setOver(false);
    void addFiles(Array.from(e.dataTransfer?.files ?? []));
  }

  const setAlt = (i: number, alt: string) => onChange(images.map((im, j) => (j === i ? { ...im, alt } : im)));
  const remove = (i: number) => onChange(images.filter((_, j) => j !== i));
  const full = images.length + busy >= MAX_IMAGES;

  return (
    <div className="fp-imged">
      <span className="t-label">Pictures (optional)</span>
      {images.length > 0 && (
        <ul className="fp-imged-list" aria-label="Pictures for this question">
          {images.map((im, i) => (
            <li key={`${im.url}-${i}`} className="fp-imged-item">
              <img src={im.url} alt={im.alt || `Picture ${i + 1}`} />
              <label htmlFor={`${idPrefix}-img-${i}-alt`} className="sr-only">
                Description of picture {i + 1}
              </label>
              <div className="fp-imged-row">
                <input
                  id={`${idPrefix}-img-${i}-alt`}
                  className="input"
                  value={im.alt}
                  maxLength={300}
                  placeholder="Describe the picture"
                  onChange={(e) => setAlt(i, e.target.value)}
                />
                <button type="button" className="t-icon-btn danger" aria-label={`Remove picture ${i + 1}`} onClick={() => remove(i)}>
                  ×
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <div
        className={`fp-imged-drop${over ? " over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!full) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <button type="button" className="t-btn-outline" disabled={full} onClick={() => fileRef.current?.click()}>
          + Add picture
        </button>
        <span>{busy > 0 ? "Uploading…" : full ? `${MAX_IMAGES} pictures (the most)` : "or drop PNG, JPEG, GIF or WebP here (5 MB max)"}</span>
        <input
          ref={fileRef}
          id={`${idPrefix}-img-file`}
          type="file"
          accept={IMAGE_TYPES.join(",")}
          multiple
          hidden
          aria-label="Choose pictures"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            void addFiles(files);
          }}
        />
      </div>
      {error && (
        <span className="field-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
