import type { OverlayMark } from "./diagramLogic";
import { STATUS_ICON, STATUS_WORD } from "./diagramLogic";
import "../../styles/diagrams.css";

/**
 * The student's drawing with the checklist marks on top: boxes / points placed in normalised
 * image coordinates, labelled ✓ / ✗ and the feature number (matches the checklist).
 */
export function DiagramOverlay({
  src,
  alt,
  marks,
  zoom = 1,
  className = "",
}: {
  src: string;
  alt: string;
  marks: OverlayMark[];
  zoom?: number;
  className?: string;
}) {
  return (
    <div className={`dg-overlay ${className}`} style={{ width: `${zoom * 100}%` }} data-testid="diagram-overlay">
      <img src={src} alt={alt} />
      <ul className="dg-marks" aria-label="Checklist marks on the drawing">
        {marks.map((m) => (
          <li
            key={m.id}
            className={`dg-mark dg-${m.kind} ${m.ok ? "is-ok" : `is-${m.status}`}`}
            style={m.style}
            title={m.title}
            data-feature={m.id}
            data-status={m.status}
          >
            <span className="dg-tag">
              <span aria-hidden="true">{STATUS_ICON[m.status]}</span> {m.n}
              <span className="sr-only">
                . {m.label}: {STATUS_WORD[m.status]}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
