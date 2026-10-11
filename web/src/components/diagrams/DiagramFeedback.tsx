import type { GradeDiagram } from "../../api/diagrams";
import { DiagramOverlay } from "./DiagramOverlay";
import { STATUS_ICON, STATUS_WORD, overlayMarks } from "./diagramLogic";
import "../../styles/diagrams.css";

function fmt(n: number): string {
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

/** Student feedback for a graph / diagram question: annotated drawing + checklist results. */
export function DiagramFeedback({ index, image, diagram }: { index: number; image: string | null; diagram: GradeDiagram }) {
  const marks = overlayMarks(diagram.features);
  return (
    <section className="dg-feedback" aria-label={`Checklist for question ${index + 1}`}>
      {image && <DiagramOverlay src={image} alt={`Your drawing for question ${index + 1}, marked`} marks={marks} />}
      <h2>What your drawing was checked for · {diagram.kind_label}</h2>
      <ol className="dg-list is-readonly">
        {diagram.features.map((f, i) => (
          <li key={f.id} className={`dg-row is-${f.status}`}>
            <span className="dg-num" aria-hidden="true">
              {i + 1}
            </span>
            <span className={`dg-status is-${f.status}`}>
              <span aria-hidden="true">{STATUS_ICON[f.status]}</span> {STATUS_WORD[f.status]}
            </span>
            <span className="dg-label">{f.label}</span>
            <b className="dg-pts">
              {fmt(f.points)} / {fmt(f.max_points)}
            </b>
            {f.note && <span className="dg-note">{f.note}</span>}
          </li>
        ))}
      </ol>
      {diagram.reference_image && (
        <details className="dg-refdetails">
          <summary>See the correct drawing</summary>
          <img src={diagram.reference_image} alt="Your teacher's correct drawing" />
        </details>
      )}
    </section>
  );
}
