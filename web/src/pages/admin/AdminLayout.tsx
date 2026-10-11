import { Link, Outlet, useNavigate } from "react-router-dom";
import { clearToken } from "../../api/client";
import { Icon, LogoMark } from "../../components/ui/Icon";
import { useMe } from "../../lib/useMe";
import "../../styles/teacher.css";
import "../../styles/admin.css";

/** Slim top bar (brand, "Admin", the signed-in name, Sign out) above the page. */
export function AdminLayout() {
  const navigate = useNavigate();
  const me = useMe("superadmin");
  const signOut = () => {
    clearToken("superadmin");
    navigate("/admin/login", { replace: true });
  };
  return (
    <div className="a-shell">
      <header className="a-top">
        <Link to="/admin" className="t-brand a-brand" aria-label="FairPlay admin home">
          <LogoMark />
          <span>FairPlay</span>
          <span className="a-badge">Admin</span>
        </Link>
        <span className="a-spacer" />
        {me && (
          <span className="a-user" data-testid="admin-user">
            {me.name}
          </span>
        )}
        <button type="button" className="t-signout a-signout" onClick={signOut}>
          <Icon name="logout" size={16} />
          Sign out
        </button>
      </header>
      <main className="a-main" id="main">
        <Outlet />
      </main>
    </div>
  );
}
