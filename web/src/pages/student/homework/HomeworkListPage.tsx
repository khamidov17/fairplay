// /student/homework — paper homework from the student's classes.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { studentHomeworkApi, type StudentHomework } from "../../../api/homework";
import { StudentLayout, StuLoading } from "../../../components/student/StudentLayout";
import { errorText } from "../../../components/teacher/util";
import "../../../styles/homework.css";
import { STATE_LABEL, dueLabel, stateTone } from "./uploadLogic";

export default function HomeworkListPage() {
  const [rows, setRows] = useState<StudentHomework[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    studentHomeworkApi.list().then(setRows, (e) => setError(errorText(e)));
  }, []);

  return (
    <StudentLayout>
      <main className="stu-main narrow">
        <div className="stu-head">
          <h1 className="stu-h1">Homework</h1>
          <p className="stu-sub">Do it on paper, then hand it in with your phone camera.</p>
        </div>
        {error && (
          <div className="stu-alert error" role="alert">
            {error}
          </div>
        )}
        {!rows && !error ? (
          <StuLoading label="Loading homework…" />
        ) : rows && rows.length === 0 ? (
          <section className="stu-empty">
            <h2>No homework</h2>
            <p className="stu-muted">When your teacher sets paper homework for your class, it shows up here.</p>
          </section>
        ) : (
          <ul className="hw-stu-list" aria-label="Homework">
            {(rows ?? []).map((h) => (
              <HomeworkRow key={h.exam_id} h={h} />
            ))}
          </ul>
        )}
      </main>
    </StudentLayout>
  );
}

export function HomeworkRow({ h }: { h: StudentHomework }) {
  const tone = stateTone(h);
  return (
    <li>
      <Link to={`/student/homework/${h.exam_id}`} className="stu-card hw-stu-item">
        <span className="hw-stu-title">{h.title}</span>
        <span className="stu-muted stu-small">
          {[h.class_names.join(", "), dueLabel(h.due_at)].filter(Boolean).join(" · ")}
        </span>
        <span className="hw-stu-tags">
          <span className={`stu-pill ${tone}`}>{h.overdue ? "Overdue" : STATE_LABEL[h.state]}</span>
          {h.late && <span className="stu-pill warn">Late</span>}
          {h.feedback_new && <span className="stu-pill info dot">New</span>}
          {h.percent != null && <span className="hw-stu-score">{h.percent}%</span>}
        </span>
      </Link>
    </li>
  );
}
