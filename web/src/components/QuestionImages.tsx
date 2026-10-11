// Slice 6 B: pictures above a question prompt. Used on the student exam page, review,
// feedback, the teacher Questions tab, submission review and grading. Each picture is a
// button that opens it large (Escape / the close button / a click outside closes it).
import { useEffect, useRef, useState } from "react";
import type { QuestionImage } from "../api/types";
import "../styles/slice6.css";

export function QuestionImages({
  images,
  compact = false,
  label = "Question pictures",
}: {
  images?: QuestionImage[] | null;
  /** Smaller thumbnails (lists, review rows). */
  compact?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const list = images ?? [];
  if (list.length === 0) return null;
  return (
    <>
      <ul className={`fp-qimgs${compact ? " compact" : ""}`} aria-label={label}>
        {list.map((img, i) => (
          <li key={`${img.url}-${i}`}>
            <button
              type="button"
              className="fp-qimg-btn"
              onClick={() => setOpen(i)}
              aria-label={`Enlarge picture ${i + 1}${img.alt ? `: ${img.alt}` : ""}`}
            >
              <img src={img.url} alt={img.alt || `Picture ${i + 1}`} loading="lazy" />
            </button>
          </li>
        ))}
      </ul>
      {open !== null && list[open] && <ImageViewer image={list[open]} onClose={() => setOpen(null)} />}
    </>
  );
}

function ImageViewer({ image, onClose }: { image: QuestionImage; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, [onClose]);
  return (
    <div className="fp-viewer" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="fp-viewer-box" role="dialog" aria-modal="true" aria-label={image.alt || "Picture"}>
        <button ref={closeRef} type="button" className="fp-viewer-close" onClick={onClose}>
          Close
        </button>
        <img src={image.url} alt={image.alt || "Picture"} />
        {image.alt && <p className="fp-viewer-alt">{image.alt}</p>}
      </div>
    </div>
  );
}

/** Small topic chips (teacher views). */
export function TopicChips({ topics }: { topics?: string[] | null }) {
  const list = topics ?? [];
  if (list.length === 0) return null;
  return (
    <ul className="fp-topic-chips" aria-label="Topics">
      {list.map((t) => (
        <li key={t}>{t}</li>
      ))}
    </ul>
  );
}
