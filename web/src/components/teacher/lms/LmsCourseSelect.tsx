import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { lmsApi } from "../../../api/client";
import type { LmsCourseLink } from "../../../api/types";

/** Exam setup: "Course in your LMS" select (slice 5). Shows nothing until the
 *  teacher has linked a course, apart from a pointer to the LMS sync page. */
export function LmsCourseSelect({
  value,
  onChange,
}: {
  value: number | null;
  onChange: (linkId: number | null, link: LmsCourseLink | null) => void;
}) {
  const [links, setLinks] = useState<LmsCourseLink[] | null>(null);

  useEffect(() => {
    let alive = true;
    lmsApi.linkedCourses().then(
      (l) => alive && setLinks(l),
      () => alive && setLinks([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  if (links === null) return null;
  if (links.length === 0 && value == null) {
    return (
      <p className="t-small t-muted" style={{ margin: 0 }}>
        Using Canvas or another LMS? <Link to="/teacher/lms">Link a course</Link> to import students and send grades
        back.
      </p>
    );
  }
  const known = value == null || links.some((l) => l.id === value);
  return (
    <div className="t-field">
      <label htmlFor="exam-lms-course">Course in your LMS</label>
      <select
        id="exam-lms-course"
        className="t-select"
        value={value == null ? "" : String(value)}
        onChange={(e) => {
          const id = e.target.value ? Number(e.target.value) : null;
          onChange(id, links.find((l) => l.id === id) ?? null);
        }}
        aria-describedby="exam-lms-course-sub"
      >
        <option value="">Not linked — any student with the code can start</option>
        {!known && <option value={String(value)}>Linked course #{value}</option>}
        {links.map((l) => (
          <option key={l.id} value={String(l.id)}>
            {l.name}
            {l.code ? ` (${l.code})` : ""} · {l.provider_name} · {l.member_count} students
          </option>
        ))}
      </select>
      <span id="exam-lms-course-sub" className="t-small t-muted">
        When linked, only students of that course can start this exam, and grades can be sent back.
      </span>
    </div>
  );
}
