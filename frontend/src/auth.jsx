import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router";
import { api, setCsrfToken, setUnauthorizedHandler } from "./api.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  // status: loading 检查中 / anon 未登录 / authed 已登录
  const [state, setState] = useState({ status: "loading", user: null });

  const signedIn = useCallback((me) => {
    setCsrfToken(me.csrf_token);
    setState({ status: "authed", user: { username: me.username, displayName: me.display_name } });
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

export function RequireAuth({ children }) {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <div className="pnl pad sub" style={{ margin: 14 }}>正在检查登录状态……</div>;
  if (status === "anon") return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return children;
}
