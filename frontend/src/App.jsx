import { Navigate, Outlet, Route, Routes, useParams } from "react-router";
import { RequireAuth } from "./auth.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Chat from "./pages/Chat.jsx";
import Login from "./pages/Login.jsx";
import Overview from "./pages/Overview.jsx";
import Projects from "./pages/Projects.jsx";

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
        <Route path="/projects" element={<Projects />} />
        <Route path="/project/:code" element={<OverviewPage />} />
        <Route path="/chat/:code" element={<ChatPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
