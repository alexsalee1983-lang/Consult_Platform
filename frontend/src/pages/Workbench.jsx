import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";
import { hasRole, ROLE_NAMES, useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";

// 待办分组：顺序即显示顺序
const GROUPS = [
  ["confirmation", "待处理的确认卡", "Agent 在交付链里遇到取舍、证据不足或范围确认，等你选择"],
  ["signoff", "待我签署的交付件", "你是项目经理的项目里已提交签署的交付件"],
  ["kb_content", "知识内容审核", "知识专家审核内容"],
  ["kb_compliance", "知识合规审核", "知识管理员审核合规"],
  ["recheck", "待补复审的知识", "公开资料由作者本人审核发布，需另一位知识专家复审"],
  ["deid", "待确认去标识", "知识管理员核对去标识报告"],
  ["deletion", "待确认的资料删除", "项目负责人确认删除申请"],
];

// 工作台：一个人身上各角色的待办汇总在一处；本人不能处理的（自己是作者、上传者、发起人）置灰，写明要谁处理
export default function Workbench() {
  const { user } = useAuth();
  const [w, setW] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { api.workbench().then(setW).catch((e) => setError(e.message)); }, []);
  const roles = (user?.roles || []).map((r) => ROLE_NAMES[r] || r).join("、");
  if (error) return <section className="pnl pad err" role="alert">{error}</section>;
  if (!w) return <section className="pnl pad sub">加载中…</section>;
  const mine = w.todos.filter((t) => !t.blocked).length;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap={roles} title="工作台" />
      <div className="sub">{mine ? `有 ${mine} 项待你处理。` : "没有待你处理的事项。"}
        {w.todos.length > mine && ` 另有 ${w.todos.length - mine} 项按规则须由别人处理，列在下面供了解。`}</div>
      {!w.member_projects && (
        <section className="pnl pad">
          {hasRole(user, "project_lead")
            ? <div className="row"><span className="sub">你还不是任何项目的成员。立项时把自己指定为项目经理，或请项目经理把你加入项目。</span>
                <Link className="btnp sm" to="/projects">新建项目</Link></div>
            : <span className="sub">你还不是任何项目的成员，请项目经理把你加入项目；知识检索和投料箱不需要项目。</span>}
        </section>
      )}
      {GROUPS.map(([kind, name, hint]) => {
        const rows = w.todos.filter((t) => t.kind === kind);
        if (!rows.length) return null;
        return (
          <section key={kind} className="pnl pad" aria-label={name}>
            <div className="sb"><h2 className="h2">{name} <span className="sub mono">{rows.filter((t) => !t.blocked).length}</span></h2>
              <span className="sub">{hint}</span></div>
            <ul className="lst">
              {rows.map((t, i) => (
                <li key={i} className="sb" style={t.blocked ? { opacity: 0.55 } : undefined}>
                  <span>{t.project_code && <span className="mono sub">{t.project_code} · </span>}{t.title}</span>
                  {t.blocked ? <span className="sub">{t.blocked}</span> : <Link className="btn sm" to={t.link}>去处理</Link>}
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
