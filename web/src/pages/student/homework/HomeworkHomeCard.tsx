// Student home: "Feedback ready" badge (in-app notification, no email) + homework still to do.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { studentHomeworkApi, type StudentHomework, type StudentNotifications } from "../../../api/homework";
import "../../../styles/homework.css";
import { dueLabel } from "./uploadLogic";

export function HomeworkHomeCard() {
  const [notes, setNotes] = useState<StudentNotifications | null>(null);
  const [todo, setTodo] = useState<StudentHomework[]>([]);

  useEffect(() => {
    let alive = true;
    studentHomeworkApi.notifications().then((n) => alive && setNotes(n), () => undefined);
    studentHomeworkApi.list().then(
      (rows) => alive && setTodo(rows.filter((h) => h.state === "not_submitted" || h.state === "failed")),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);

  if (!notes?.feedback_ready && todo.length === 0) return null;
  return (
    <section className="stu-card hw-home" aria-labelledby="hw-home-h">
      <div className="stu-row">
        <h2 id="hw-home-h">Homework</h2>
        {notes && notes.feedback_ready > 0 && (
          <span className="stu-pill info dot hw-badge" role="status">
            Feedback ready{notes.feedback_ready > 1 ? ` (${notes.feedback_ready})` : ""}
          </span>
        )}
        <span className="stu-spacer" />
        <Link to="/student/homework" className="stu-link">
          All homework →
        </Link>
      </div>
      <ul className="hw-home-list">
        {notes?.items.slice(0, 3).map((n) => (
          <li key={n.exam_id}>
            <Link to={`/student/homework/${n.exam_id}`}>{n.title}</Link>
            <span className="stu-pill info">Feedback ready</span>
          </li>
        ))}
        {todo.slice(0, 3).map((h) => (
          <li key={h.exam_id}>
            <Link to={`/student/homework/${h.exam_id}`}>{h.title}</Link>
            <span className={`stu-pill ${h.overdue ? "danger" : "warn"}`}>{dueLabel(h.due_at)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
