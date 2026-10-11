import { useEffect } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { loginPath, setUnauthorizedHandler } from "./api/client";
import { hello } from "./lib/bridge";
import TeacherRoutes from "./pages/teacher/routes";
import StudentRoutes from "./pages/student/routes";
import AdminRoutes from "./pages/admin/routes";
import RegisterPage from "./pages/RegisterPage";

export default function App() {
  const navigate = useNavigate();
  useEffect(() => {
    setUnauthorizedHandler((role) => navigate(loginPath(role), { replace: true }));
  }, [navigate]);
  useEffect(() => {
    // Native bridge: announce ourselves; the Mac app answers with its state.
    hello();
  }, []);

  return (
    <Routes>
      <Route path="/" element={<Navigate to="/teacher" replace />} />
      <Route path="/teacher/*" element={<TeacherRoutes />} />
      <Route path="/student/*" element={<StudentRoutes />} />
      <Route path="/admin/*" element={<AdminRoutes />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="*" element={<Navigate to="/teacher" replace />} />
    </Routes>
  );
}
