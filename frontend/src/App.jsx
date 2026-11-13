import { useEffect, useState } from "react";
import { Navigate, Outlet, Route, Routes, useParams } from "react-router";
import { api } from "./api.js";
import { HomeRedirect, PASSWORD_PATH, RequireAuditor, RequireAuth, RequireBusiness, RequireKnowledge, RequireLead, RequireSysAdmin } from "./auth.jsx";
import { Loading } from "./components/common.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Audit from "./pages/Audit.jsx";
import Chat from "./pages/Chat.jsx";
import Deletions from "./pages/Deletions.jsx";
import Deliverable from "./pages/Deliverable.jsx";
import KbIntake from "./pages/KbIntake.jsx";
import KbMap from "./pages/KbMap.jsx";
import KbReview from "./pages/KbReview.jsx";
import KbSearch from "./pages/KbSearch.jsx";
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

function DeliverablePage() {
  const { code, dcode } = useParams();
  return <Deliverable code={code} dcode={dcode} />;
}

// 侧栏“交付件审阅”入口：先打开待签署的，其次起草中的，否则 D1
function DeliverableHome() {
  const { code } = useParams();
  const [target, setTarget] = useState(null);
  useEffect(() => {
    api.project(code).then((p) => {
      const pick = ["review", "drafting"].map((st) => p.deliverables.find((d) => d.status === st)).find(Boolean)
        || p.deliverables[0];
      setTarget(pick ? pick.code : "");
    }).catch(() => setTarget(""));
  }, [code]);
  if (target === null) return <Loading />;
  if (!target) return <Navigate to="/projects" replace />;
  return <Navigate to={`/project/${encodeURIComponent(code)}/deliverables/${target}`} replace />;
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
          <Route path="/project/:code/deliverables" element={<DeliverableHome />} />
          <Route path="/project/:code/deliverables/:dcode" element={<DeliverablePage />} />
          <Route path="/chat/:code" element={<ChatPage />} />
          <Route path="/deletions" element={<RequireLead><Deletions /></RequireLead>} />
          <Route path="/kb/search" element={<KbSearch />} />
          <Route path="/kb/intake" element={<KbIntake />} />
          <Route path="/kb/review" element={<RequireKnowledge><KbReview /></RequireKnowledge>} />
          <Route path="/kb/map" element={<RequireKnowledge><KbMap /></RequireKnowledge>} />
        </Route>
        <Route path="/audit" element={<RequireAuditor><Audit /></RequireAuditor>} />
        <Route path="/admin/users" element={<RequireSysAdmin><Users /></RequireSysAdmin>} />
        <Route path={PASSWORD_PATH} element={<Password />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
