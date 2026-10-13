import type { ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { clearToken } from "../../api/client";
import { useFeatures, type Feature } from "../../lib/features";
import { useMe } from "../../lib/useMe";
import { Icon, LogoMark, type IconName } from "../ui/Icon";
import "../../styles/teacher.css";

type Section = "home" | "exams" | "classes" | "grading" | "gradebook" | "lms" | "insights" | "students" | "practice" | "scan";

/** Which nav item is active for a path. */
export function sectionFor(pathname: string): Section | null {
  const p = pathname.replace(/\/+$/, "");
  if (p === "/teacher") return "home";
  if (p.startsWith("/teacher/gradebook")) return "gradebook";
  if (p.startsWith("/teacher/lms")) return "lms";
  if (p.startsWith("/teacher/insights")) return "insights";
  if (p.startsWith("/teacher/students")) return "students";
  if (p.startsWith("/teacher/classes")) return "classes";
  if (p.startsWith("/teacher/practice")) return "practice";
  if (p.startsWith("/teacher/scan") || /^\/teacher\/exams\/[^/]+\/scan/.test(p)) return "scan";
  if (p.startsWith("/teacher/grading") || /^\/teacher\/exams\/[^/]+\/grading/.test(p)) return "gradebook";
  if (p.startsWith("/teacher/exams")) return "exams";
  return null;
}

// 2026-10-13: one "Grades" page (grade, release, marks). Optional parts only when the
// server switches them on (lib/features.ts).
const NAV: { id: Section; to: string; label: string; icon: IconName; feature?: Feature }[] = [
  { id: "home", to: "/teacher", label: "Home", icon: "home" },
  { id: "exams", to: "/teacher/exams", label: "Exams", icon: "exams" },
  { id: "classes", to: "/teacher/classes", label: "Classes", icon: "classes" },
  { id: "gradebook", to: "/teacher/gradebook", label: "Grades", icon: "gradebook" },
  { id: "students", to: "/teacher/students", label: "Students", icon: "students" },
  { id: "scan", to: "/teacher/scan", label: "Scan & Grade", icon: "scan", feature: "scan" },
  { id: "insights", to: "/teacher/insights", label: "Insights", icon: "insights", feature: "insights" },
  { id: "practice", to: "/teacher/practice", label: "Practice", icon: "practice", feature: "practice" },
  { id: "lms", to: "/teacher/lms", label: "LMS sync", icon: "sync", feature: "lms" },
];

export function TeacherLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const me = useMe("teacher");
  const active = sectionFor(pathname);
  const features = useFeatures();

  const signOut = () => {
    clearToken("teacher");
    navigate("/teacher/login", { replace: true });
  };

  return (
    <div className="t-shell">
      <nav className="t-nav" aria-label="Main">
        <Link to="/teacher" className="t-brand" aria-label="FairPlay home">
          <LogoMark />
          <span>FairPlay</span>
        </Link>
        {NAV.filter((item) => !item.feature || features.has(item.feature)).map((item) => (
          <Link
            key={item.id}
            to={item.to}
            className="t-nav-link"
            aria-current={active === item.id ? "page" : undefined}
          >
            <Icon name={item.icon} />
            {item.label}
          </Link>
        ))}
        <div className="t-nav-spacer" />
        <div className="t-nav-user">
          {me && (
            <>
              <span className="t-nav-user-name">{me.name}</span>
              <span className="muted">Teacher · {me.username}</span>
            </>
          )}
          <button type="button" className="t-signout" onClick={signOut}>
            <Icon name="logout" size={16} />
            Sign out
          </button>
        </div>
      </nav>
      <main className="t-main" id="main">
        <Outlet />
      </main>
    </div>
  );
}

/** Status pill used across teacher tables. */
export function Pill({ tone = "grey", live, children }: { tone?: string; live?: boolean; children: ReactNode }) {
  return <span className={`t-pill ${tone}${live ? " live" : ""}`}>{children}</span>;
}
