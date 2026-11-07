import { useEffect, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import Projects from "./pages/Projects.jsx";
import Overview from "./pages/Overview.jsx";
import Chat from "./pages/Chat.jsx";

// 轻量哈希路由：#/projects、#/project/BANK-A、#/chat/BANK-A
function parse() {
  const [, page = "projects", code = "BANK-A"] = (window.location.hash || "#/projects").split("/");
  return { page, code: decodeURIComponent(code) };
}

export default function App() {
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  let page;
  if (route.page === "project") page = <Overview code={route.code} />;
  else if (route.page === "chat") page = <Chat code={route.code} />;
  else page = <Projects />;

  return (
    <div className="layout">
      <Sidebar page={route.page} code={route.code} />
      <main className="content">{page}</main>
    </div>
  );
}
