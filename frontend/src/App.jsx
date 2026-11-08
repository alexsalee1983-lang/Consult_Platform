import { Navigate, Outlet, Route, Routes, useParams } from "react-router";
import { HomeRedirect, PASSWORD_PATH, RequireAuditor, RequireAuth, RequireBusiness, RequireSysAdmin } from "./auth.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Audit from "./pages/Audit.jsx";
import Chat from "./pages/Chat.jsx";
import Login from "./pages/Login.jsx";
import Overview from "./pages/Overview.jsx";
import Password from "./pages/Password.jsx";
import Projects from "./pages/Projects.jsx";
import Users from "./pages/Users.jsx";

function Shell() {
  return (
    <RequireAuth>
      <div className="layout">
        <Sidebar />
        <main className="content"><Outlet /></main>
      </div>
    </RequireAuth>
  );
}

// 业务页面：只有业务角色能看（三权分立）
function Business() {
  return <RequireBusiness><Outlet /></RequireBusiness>;
}

function OverviewPage() {
  const { code } = useParams();
  return <Overview code={code} />;
}

function ChatPage() {
  const { code } = useParams();
  return <Chat code={code} />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Shell />}>
        <Route index element={<HomeRedirect />} />
        <Route element={<Business />}>
          <Route path="/projects" element={<Projects />} />
          <Route path="/project/:code" element={<OverviewPage />} />
          <Route path="/chat/:code" element={<ChatPage />} />
        </Route>
        <Route path="/audit" element={<RequireAuditor><Audit /></RequireAuditor>} />
        <Route path="/admin/users" element={<RequireSysAdmin><Users /></RequireSysAdmin>} />
        <Route path={PASSWORD_PATH} element={<Password />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
