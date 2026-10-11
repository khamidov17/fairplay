// Teacher routes (owned by the teacher UI). Mounted at /teacher/* in App.tsx.
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "../../components/RequireAuth";
import { TeacherLayout } from "../../components/teacher/TeacherLayout";
import LoginPage from "../LoginPage";
import TeacherHome from "./TeacherHome";
import ExamsPage from "./ExamsPage";
import ExamSetupPage from "./ExamSetupPage";
import TeacherExamPage from "./TeacherExamPage";
import GradingPage from "./GradingPage";
import GradingListPage from "./GradingListPage";
import GradebookPage from "./GradebookPage";
import LmsPage from "./LmsPage";
import SubmissionReviewPage from "./SubmissionReviewPage";
import InsightsPage from "./InsightsPage";
import StudentsPage from "./StudentsPage";
import StudentProfilePage from "./StudentProfilePage";
import PracticePage from "./PracticePage";
import ScanOverviewPage from "./scan/ScanOverviewPage";
import ScanExamPage from "./scan/ScanExamPage";
import SessionTimelinePage from "./SessionTimelinePage";

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
        <Route path="exams/:id/submissions/:attemptId/timeline" element={<SessionTimelinePage />} />
        <Route path="grading" element={<GradingListPage />} />
        <Route path="gradebook" element={<GradebookPage />} />
        <Route path="lms" element={<LmsPage />} />
        <Route path="insights" element={<InsightsPage />} />
        <Route path="students" element={<StudentsPage />} />
        <Route path="students/:username" element={<StudentProfilePage />} />
        <Route path="practice" element={<PracticePage />} />
        <Route path="scan" element={<ScanOverviewPage />} />
        <Route path="exams/:id/scan" element={<ScanExamPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/teacher" replace />} />
    </Routes>
  );
}
