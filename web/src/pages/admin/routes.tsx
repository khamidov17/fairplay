// Superadmin routes. Mounted at /admin/* in App.tsx.
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "../../components/RequireAuth";
import LoginPage from "../LoginPage";
import AdminDashboard from "./AdminDashboard";
import { AdminLayout } from "./AdminLayout";

export default function AdminRoutes() {
  return (
    <Routes>
      <Route path="login" element={<LoginPage role="superadmin" />} />
      <Route
        element={
          <RequireAuth role="superadmin">
            <AdminLayout />
          </RequireAuth>
        }
      >
        <Route index element={<AdminDashboard />} />
      </Route>
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  );
}
