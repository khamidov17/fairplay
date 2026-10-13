// Teacher routes (owned by the teacher UI). Mounted at /teacher/* in App.tsx.
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "../../components/RequireAuth";
import { TeacherLayout } from "../../components/teacher/TeacherLayout";
import { FeatureGate } from "../../components/FeatureGate";
import LoginPage from "../LoginPage";
import TeacherHome from "./TeacherHome";
import ExamsPage from "./ExamsPage";
import ExamSetupPage from "./ExamSetupPage";
import TeacherExamPage from "./TeacherExamPage";
import GradingPage from "./GradingPage";
import GradebookPage from "./GradebookPage";
import LmsPage from "./LmsPage";
import SubmissionReviewPage from "./SubmissionReviewPage";
import InsightsPage from "./InsightsPage";
import StudentsPage from "./StudentsPage";
import ClassesPage from "./ClassesPage";
import ClassPage from "./ClassPage";
import StudentProfilePage from "./StudentProfilePage";
import PracticePage from "./PracticePage";
import ScanOverviewPage from "./scan/ScanOverviewPage";
import ScanExamPage from "./scan/ScanExamPage";

export default function TeacherRoutes() {
  return (
    <Routes>
      <Route path="login" element={<LoginPage role="teacher" />} />
      <Route
        element={
          <RequireAuth role="teacher">
            <TeacherLayout />
          </RequireAuth>
        }
      >
        <Route index element={<TeacherHome />} />
        <Route path="exams" element={<ExamsPage />} />
        <Route path="exams/new" element={<ExamSetupPage />} />
        <Route path="exams/:id" element={<TeacherExamPage />} />
        <Route path="exams/:id/edit" element={<ExamSetupPage />} />
        <Route path="exams/:id/grading" element={<GradingPage />} />
        <Route path="exams/:id/submissions/:attemptId" element={<SubmissionReviewPage />} />
        {/* 2026-10-13: grading list + gradebook are one "Grades" page */}
        <Route path="grading" element={<Navigate to="/teacher/gradebook" replace />} />
        <Route path="gradebook" element={<GradebookPage />} />
        <Route path="classes" element={<ClassesPage />} />
        <Route path="classes/:classId" element={<ClassPage />} />
        <Route path="students" element={<StudentsPage />} />
        <Route path="students/:username" element={<StudentProfilePage />} />
        {/* Optional parts: only when the server switches them on (FAIRPLAY_FEATURES). */}
        <Route path="lms" element={<FeatureGate feature="lms" fallback="/teacher"><LmsPage /></FeatureGate>} />
        <Route path="insights" element={<FeatureGate feature="insights" fallback="/teacher"><InsightsPage /></FeatureGate>} />
        <Route path="practice" element={<FeatureGate feature="practice" fallback="/teacher"><PracticePage /></FeatureGate>} />
        <Route path="scan" element={<FeatureGate feature="scan" fallback="/teacher"><ScanOverviewPage /></FeatureGate>} />
        <Route
          path="exams/:id/scan"
          element={<FeatureGate feature="scan" fallback="/teacher/exams"><ScanExamPage /></FeatureGate>}
        />
      </Route>
      <Route path="*" element={<Navigate to="/teacher" replace />} />
    </Routes>
  );
}
