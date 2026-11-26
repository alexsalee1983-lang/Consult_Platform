// 右侧抽屉（四期 A25②）：轻操作就地办完，重操作跳整页。用浏览器原生 <dialog open>，Esc 或 ✕ 关闭；
// 底部“查看全部”跳到对应子页面。
import { useEffect } from "react";
import { Link } from "react-router";

export default function Drawer({ title, onClose, more, children }) {
  useEffect(() => {
    const esc = (e) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <dialog open className="drawer pnl pad" aria-label={title}>
      <div className="sb">
        <h2 className="h2">{title}</h2>
        <button type="button" className="btn sm" aria-label="关闭" onClick={onClose}>✕</button>
      </div>
      <div className="drawer-body">{children}</div>
      {more && <Link className="sub" to={more.to} onClick={onClose}>{more.label} →</Link>}
    </dialog>
  );
}
