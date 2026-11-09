import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router";
import { api, setCsrfToken, setUnauthorizedHandler } from "./api.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  // status: loading 检查中 / anon 未登录 / authed 已登录
  const [state, setState] = useState({ status: "loading", user: null });

  const signedIn = useCallback((me) => {
    setCsrfToken(me.csrf_token);
    setState({ status: "authed", user: {
      username: me.username, displayName: me.display_name, roles: me.roles || [],
      mustChangePassword: Boolean(me.must_change_password),
    } });
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
    // 改口令后服务端换了新会话，用返回的 me 刷新 CSRF 令牌和“须改口令”状态
    changePassword: async (oldPassword, newPassword) => signedIn(await api.changePassword(oldPassword, newPassword)),
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

export function hasRole(user, role) {
  return (user?.roles || []).includes(role);
}

// 知识专家、知识管理员：能看审批队列、知识地图，能编辑和审核知识条目
export function isKnowledge(user) {
  return hasRole(user, "knowledge_expert") || hasRole(user, "knowledge_admin");
}

export function isSysAdmin(user) {
  return (user?.roles || []).includes("sys_admin");
}

// 登录后的首页：审计员去审计日志，系统管理员去用户与角色，其他人去我的项目
export function homePath(user) {
  if (isAuditor(user)) return "/audit";
  if (isSysAdmin(user)) return "/admin/users";
  return "/projects";
}

export function HomeRedirect() {
  const { user } = useAuth();
  return <Navigate to={homePath(user)} replace />;
}

export function RequireKnowledge({ children }) {
  const { user } = useAuth();
  if (isKnowledge(user)) return children;
  return (
    <section className="pnl pad" aria-label="无权限">
      <h1 className="h1">只有知识专家、知识管理员能使用这个页面</h1>
      <div className="sub" style={{ fontSize: 13 }}>顾问可以在“投料箱”提交资料，在“知识检索”查已发布的知识。</div>
    </section>
  );
}

export function RequireSysAdmin({ children }) {
  const { user } = useAuth();
  if (isSysAdmin(user)) return children;
  return (
    <section className="pnl pad" aria-label="无权限">
      <h1 className="h1">只有系统管理员能管理账号和角色</h1>
      <div className="sub" style={{ fontSize: 13 }}>需要开通账号、调整角色或重置口令，请联系系统管理员。</div>
    </section>
  );
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
        {isSysAdmin(user) ? "请在“用户与角色”页管理账号。" : ""}
      </div>
    </section>
  );
}

export const PASSWORD_PATH = "/account/password";

export function RequireAuth({ children }) {
  const { status, user } = useAuth();
  const location = useLocation();
  if (status === "loading") return <div className="pnl pad sub" style={{ margin: 14 }}>正在检查登录状态……</div>;
  if (status === "anon") return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  // 初始口令或被重置的口令：先改口令，其他接口服务端也会拒绝
  if (user?.mustChangePassword && location.pathname !== PASSWORD_PATH) return <Navigate to={PASSWORD_PATH} replace />;
  return children;
}
