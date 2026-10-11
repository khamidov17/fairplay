// Student routes (owned by the student UI). Mounted at /student/* in App.tsx.
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "../../components/RequireAuth";
import LoginPage from "../LoginPage";
import StudentHome from "./StudentHome";
import ExamsPage from "./ExamsPage";
import CheckInPage from "./CheckInPage";
import ExamTakingPage from "./ExamTakingPage";
import DonePage from "./DonePage";
import GradesPage from "./GradesPage";
import FeedbackPage from "./FeedbackPage";
import StudentPracticePage from "./PracticePage";
import ExternalExamPage from "./ExternalExamPage";
import ExternalDonePage from "./ExternalDonePage";
import ClassesPage from "./ClassesPage";

export default function StudentRoutes() {
  return (
    <Routes>
      <Route path="login" element={<LoginPage role="student" />} />
      <Route index element={<RequireAuth role="student"><StudentHome /></RequireAuth>} />
      <Route path="exams" element={<RequireAuth role="student"><ExamsPage /></RequireAuth>} />
      <Route path="exams/:examId/checkin" element={<RequireAuth role="student"><CheckInPage /></RequireAuth>} />
      <Route path="exam/:attemptId" element={<RequireAuth role="student"><ExamTakingPage /></RequireAuth>} />
      <Route path="external/:attemptId" element={<RequireAuth role="student"><ExternalExamPage /></RequireAuth>} />
      <Route path="external-done/:attemptId" element={<RequireAuth role="student"><ExternalDonePage /></RequireAuth>} />
      <Route path="done/:attemptId" element={<RequireAuth role="student"><DonePage /></RequireAuth>} />
      <Route path="grades" element={<RequireAuth role="student"><GradesPage /></RequireAuth>} />
      <Route path="grades/:examId" element={<RequireAuth role="student"><FeedbackPage /></RequireAuth>} />
      <Route path="practice" element={<RequireAuth role="student"><StudentPracticePage /></RequireAuth>} />
      <Route path="practice/:setId" element={<RequireAuth role="student"><StudentPracticePage /></RequireAuth>} />
      <Route path="classes" element={<RequireAuth role="student"><ClassesPage /></RequireAuth>} />
      <Route path="*" element={<Navigate to="/student" replace />} />
    </Routes>
  );
}
