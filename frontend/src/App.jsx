import { useEffect, useState } from "react";
import { Navigate, Outlet, Route, Routes, useParams, useSearchParams } from "react-router";
import { api } from "./api.js";
import { HomeRedirect, PASSWORD_PATH, RequireAuditor, RequireAuth, RequireBusiness, RequireKnowledge, RequireLead, RequireSysAdmin } from "./auth.jsx";
import { Loading } from "./components/common.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Audit from "./pages/Audit.jsx";
import Chat from "./pages/Chat.jsx";
import Deletions from "./pages/Deletions.jsx";
import Portfolio from "./pages/Portfolio.jsx";
import Indicators from "./pages/Indicators.jsx";
import QualityIncidents from "./pages/QualityIncidents.jsx";
import Workbench from "./pages/Workbench.jsx";
import MyTasks from "./pages/MyTasks.jsx";
import ExpertBench from "./pages/ExpertBench.jsx";
import Deliverable from "./pages/Deliverable.jsx";
import KbBatches from "./pages/KbBatches.jsx";
import KbIntake from "./pages/KbIntake.jsx";
import KbMap from "./pages/KbMap.jsx";
import KbReadiness from "./pages/KbReadiness.jsx";
import KbUsage from "./pages/KbUsage.jsx";
import KbReview from "./pages/KbReview.jsx";
import KbSearch from "./pages/KbSearch.jsx";
import KbSources from "./pages/KbSources.jsx";
import Login from "./pages/Login.jsx";
import Overview from "./pages/Overview.jsx";
import Password from "./pages/Password.jsx";
import Projects from "./pages/Projects.jsx";
import { AnalysisPage, InputPage, OutputPage, ProjectLayout, SettingsPage, SurveyFull } from "./pages/ProjectPages.jsx";
import Models from "./pages/Models.jsx";
import RuntimeConfig from "./pages/RuntimeConfig.jsx";
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
  const [q] = useSearchParams();
  return <Overview key={`${code}:${q.get("focus") || ""}`} code={code} focus={q.get("focus") || undefined} />;
}

// 项目内导航页：路由参数 code 传给页面组件（页面组件不依赖路由，便于组件测试）
function withCode(Page) {
  return function ProjectRoute() {
    const { code } = useParams();
    return <Page code={code} />;
  };
}
const Input = withCode(InputPage), Analysis = withCode(AnalysisPage), Output = withCode(OutputPage),
  Settings = withCode(SettingsPage), SurveyPage = withCode(SurveyFull);

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
      <Route path="/project/:code/survey" element={<RequireAuth><RequireBusiness><SurveyPage /></RequireBusiness></RequireAuth>} />
      <Route element={<Shell />}>
        <Route index element={<HomeRedirect />} />
        <Route element={<Business />}>
          <Route path="/workbench" element={<Workbench />} />
          <Route path="/my-tasks" element={<MyTasks />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/project/:code" element={<ProjectLayout />}>
            <Route index element={<OverviewPage />} />
            <Route path="input" element={<Input />} />
            <Route path="analysis" element={<Analysis />} />
            <Route path="output" element={<Output />} />
            <Route path="settings" element={<Settings />} />
            <Route path="deliverables" element={<DeliverableHome />} />
            <Route path="deliverables/:dcode" element={<DeliverablePage />} />
          </Route>
          <Route path="/chat/:code" element={<ChatPage />} />
          <Route path="/deletions" element={<RequireLead><Deletions /></RequireLead>} />
          <Route path="/portfolio" element={<RequireLead><Portfolio /></RequireLead>} />
          <Route path="/indicators" element={<RequireLead><Indicators /></RequireLead>} />
          <Route path="/quality-incidents" element={<RequireLead><QualityIncidents /></RequireLead>} />
          <Route path="/kb/search" element={<KbSearch />} />
          <Route path="/kb/intake" element={<KbIntake />} />
          <Route path="/kb/review" element={<RequireKnowledge><KbReview /></RequireKnowledge>} />
          <Route path="/kb/bench" element={<RequireKnowledge><ExpertBench /></RequireKnowledge>} />
          <Route path="/kb/map" element={<RequireKnowledge><KbMap /></RequireKnowledge>} />
          <Route path="/kb/batches" element={<KbBatches />} />
          <Route path="/kb/readiness" element={<KbReadiness />} />
          <Route path="/kb/usage" element={<RequireKnowledge><KbUsage /></RequireKnowledge>} />
          <Route path="/kb/sources" element={<RequireKnowledge><KbSources /></RequireKnowledge>} />
        </Route>
        <Route path="/audit" element={<RequireAuditor><Audit /></RequireAuditor>} />
        <Route path="/admin/users" element={<RequireSysAdmin><Users /></RequireSysAdmin>} />
        <Route path="/admin/models" element={<RequireSysAdmin><Models /></RequireSysAdmin>} />
        <Route path="/admin/runtime-config" element={<RequireSysAdmin><RuntimeConfig /></RequireSysAdmin>} />
        <Route path={PASSWORD_PATH} element={<Password />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
