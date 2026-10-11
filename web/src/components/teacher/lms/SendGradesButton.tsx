import { useEffect, useState } from "react";
import { lmsApi } from "../../../api/client";
import type { Exam, LmsPushResult } from "../../../api/types";
import { errorText, plural } from "../util";

export function pushSummary(r: LmsPushResult, lmsName: string): string {
  const parts = [`Sent ${plural(r.pushed, "grade")} to ${lmsName}`];
  if (r.skipped) parts.push(`${r.skipped} skipped (not submitted or not in the LMS course)`);
  if (r.errors.length) parts.push(`${r.errors.length} failed`);
  return parts.join(" · ") + ".";
}

/** Results header: "Send grades to <LMS>" when the exam is linked and grades are released. */
export function SendGradesButton({ exam, released }: { exam: Exam; released: boolean }) {
  const linkId = exam.course_link_id ?? null;
  const [lmsName, setLmsName] = useState("LMS");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<LmsPushResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (linkId == null) return;
    let alive = true;
    lmsApi.linkedCourses().then(
      (links) => {
        const l = links.find((x) => x.id === linkId);
        if (alive && l) setLmsName(l.provider_name);
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [linkId]);

  if (linkId == null || !released) return null;

  async function send() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await lmsApi.pushGrades(exam.id));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="t-btn-outline" onClick={send} disabled={busy}>
        {busy ? `Sending to ${lmsName}…` : `Send grades to ${lmsName}`}
      </button>
      {(result || error) && (
        <span
          role={error || result?.errors.length ? "alert" : "status"}
          className={`t-small ${error || result?.errors.length ? "t-inline-error" : "t-muted"}`}
          style={{ flexBasis: "100%", textAlign: "right" }}
        >
          {error ?? (result && pushSummary(result, lmsName))}
          {result?.errors.slice(0, 5).map((e) => (
            <span key={e.student} style={{ display: "block" }}>
              {e.student}: {e.detail}
            </span>
          ))}
        </span>
      )}
    </>
  );
}
