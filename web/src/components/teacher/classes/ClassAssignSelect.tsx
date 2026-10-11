import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { classesApi, type ClassSummary } from "../../../api/classes";
import { Icon } from "../../ui/Icon";
import { plural } from "../util";
import "../../../styles/classes.css";

export const OPEN_WARNING = "Not assigned to any class: any student with the start code can join.";

type Option = Pick<ClassSummary, "id" | "name" | "section" | "term" | "archived" | "member_count">;

/** "Assign to classes" multi-select (checkbox list) for the exam editor and exam page.
 *  Loads the teacher's active classes, plus the exam's current classes when `examId` is
 *  given. `value` null = not known yet: the component reports the exam's current classes
 *  through onChange(ids, true) once loaded. */
export function ClassAssignSelect({
  examId,
  value,
  onChange,
}: {
  examId?: string;
  value: number[] | null;
  onChange: (ids: number[], initial?: boolean) => void;
}) {
  const [options, setOptions] = useState<Option[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([classesApi.list(), examId ? classesApi.examClasses(examId) : Promise.resolve(null)]).then(
      ([list, current]) => {
        if (!alive) return;
        const opts: Option[] = [...list];
        for (const c of current?.classes ?? []) if (!opts.some((o) => o.id === c.id)) opts.push(c);
        setOptions(opts);
        if (value === null) onChange((current?.classes ?? []).map((c) => c.id), true);
      },
      () => {
        if (!alive) return;
        setError(true);
        setOptions([]);
      },
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [examId]);

  if (options === null) return null;
  const selected = value ?? [];
  const toggle = (id: number, on: boolean) =>
    onChange(on ? [...selected.filter((x) => x !== id), id] : selected.filter((x) => x !== id));

  return (
    <fieldset className="t-field" style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
      <legend className="t-label" style={{ padding: 0, marginBottom: 4 }}>
        Assign to classes
      </legend>
      {error ? (
        <span className="t-small t-muted">Could not load your classes. You can assign them later on the exam page.</span>
      ) : options.length === 0 ? (
        <span className="t-small t-muted">
          No classes yet. <Link to="/teacher/classes">Create a class</Link> to give this exam to a group of students.
        </span>
      ) : (
        <div className="cl-assign">
          {options.map((c) => (
            <label key={c.id} className="t-check">
              <input
                type="checkbox"
                checked={selected.includes(c.id)}
                onChange={(e) => toggle(c.id, e.target.checked)}
              />
              <span>
                {c.name}
                {c.section ? ` · ${c.section}` : ""}
                {c.archived ? " (archived)" : ""}
              </span>
              <span className="t-muted t-small">{plural(c.member_count, "student")}</span>
            </label>
          ))}
        </div>
      )}
      {selected.length === 0 ? (
        <div className="cl-warn" role="note">
          <Icon name="info" size={18} />
          <span>{OPEN_WARNING}</span>
        </div>
      ) : (
        <span className="t-small t-muted">Only students in the selected {selected.length === 1 ? "class" : "classes"} can see and start this exam.</span>
      )}
    </fieldset>
  );
}
