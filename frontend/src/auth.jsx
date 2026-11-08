import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router";
import { api, setCsrfToken, setUnauthorizedHandler } from "./api.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  // status: loading 检查中 / anon 未登录 / authed 已登录
  const [state, setState] = useState({ status: "loading", user: null });

  const signedIn = useCallback((me) => {
    setCsrfToken(me.csrf_token);
    setState({ status: "authed", user: { username: me.username, displayName: me.display_name, roles: me.roles || [] } });
  }, []);

  const signedOut = useCallback(() => {
    setCsrfToken("");
    setState({ status: "anon", user: null });
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(signedOut); // 任何接口返回 401（会话超时等）都回到登录页
    api.me().then(signedIn).catch(signedOut);
  }, [signedIn, signedOut]);

  const value = useMemo(() => ({
    ...state,
    login: async (username, password, totp) => signedIn(await api.login(username, password, totp)),
    logout: async () => {
      try { await api.logout(); } finally { signedOut(); }
    },
  }), [state, signedIn, signedOut]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}

// 与后端 app/core/permissions.py 一致
export const ROLE_NAMES = {
  consultant: "顾问", project_lead: "项目负责人", knowledge_expert: "知识专家",
  knowledge_admin: "知识管理员", sys_admin: "系统管理员", security_auditor: "安全审计员",
};
const BUSINESS_ROLES = ["consultant", "project_lead", "knowledge_expert", "knowledge_admin"];

export function hasBusinessRole(user) {
  return (user?.roles || []).some((r) => BUSINESS_ROLES.includes(r));
}

export function isAuditor(user) {
  return (user?.roles || []).includes("security_auditor");
}

// 登录后的首页：审计员去审计日志，其他人去我的项目
export function HomeRedirect() {
  const { user } = useAuth();
  return <Navigate to={isAuditor(user) ? "/audit" : "/projects"} replace />;
}

export function RequireAuditor({ children }) {
  const { user } = useAuth();
  if (isAuditor(user)) return children;
  return (
    <section className="pnl pad" aria-label="无权限">
      <h1 className="h1">只有安全审计员能查看审计日志</h1>
      <div className="sub" style={{ fontSize: 13 }}>审计员由单独任命，在服务器上授予。</div>
    </section>
  );
}

// 业务页面：系统管理员、安全审计员、未分配角色的账号看到说明，而不是报错
export function RequireBusiness({ children }) {
  const { user } = useAuth();
  if (hasBusinessRole(user)) return children;
  const roles = (user?.roles || []).map((r) => ROLE_NAMES[r] || r).join("、");
  return (
    <section className="pnl pad" aria-label="无业务权限">
      <h1 className="h1">没有业务数据访问权限</h1>
      <div className="sub" style={{ fontSize: 13 }}>
        {roles
          ? `你的账号是${roles}。按三权分立要求，系统管理员和安全审计员看不到项目、对话等业务数据。`
          : "你的账号还没有分配角色，请联系系统管理员。"}
        账号管理和审计查询界面在后续版本提供。
      </div>
    </section>
  );
}

export function RequireAuth({ children }) {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <div className="pnl pad sub" style={{ margin: 14 }}>正在检查登录状态……</div>;
  if (status === "anon") return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return children;
}
