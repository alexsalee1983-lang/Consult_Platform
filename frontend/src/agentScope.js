// “查看 Agent 运行情况”按键按角色可见（四期 A29，《平台规划刷新设计》2.2）。纯函数，接口权限以后端为准。
// 知识侧运行记录：知识管理员（可重跑、暂停来源）、咨询业务负责人（只读）；跨项目运行状态：咨询业务负责人（只看状态）；
// 项目侧：在项目页里的成员（项目经理可重跑、暂停，成员只读，非成员进不了项目）；知识专家没有这个按键。
export function agentScopes(roles, inProject) {
  const r = new Set(roles || []);
  return {
    knowledge: r.has("knowledge_admin") || r.has("project_lead"),
    manageKnowledge: r.has("knowledge_admin"),
    portfolio: r.has("project_lead"),
    project: Boolean(inProject),
  };
}

export const showAgentButton = (s) => s.knowledge || s.portfolio || s.project;
