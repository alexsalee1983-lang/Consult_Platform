"""收录标准与判定（四期 B43，平台规划刷新设计第六节 6.2，知识中心改版方案第四节 4.1）。

- 提炼按类型套固定问题（QUESTIONS）：公司做法、常见坑、经验数值、方法模板、黄金样例各有必须回答的问题，
  答不出来的不提炼；知识编写 Agent 起草时照着答，知识审核 Agent 逐题判断草稿答没答（answers）。
- 6 条收录标准（CRITERIA）：可复用、具体可操作、有依据或证据、正确不冲突、有新增价值、合规。
- 判定只有四种（VERDICTS）：收录、合并为印证、退回补充、不收。
- decide() 由程序给出下限：程序规则没过、类型问题没答全、七项检查没过、不合规 → 至少“退回补充”；不可复用 → “不收”；
  没有新增价值 → “合并为印证”；Agent 的判定只能比下限更严，不能更松。
- KB_INTAKE_VERDICT_LIVE（默认关）：关时判定只记录，非“收录”的不自动发布、转人工；开时（评测达标后负责人打开）
  “不收 / 合并为印证 / 退回补充”由平台直接退回，Agent 漏答收录标准或类型问题的也不算“收录”。
只出键名、标签和理由，不出正文；审计只记判定和没过的键。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import meta

CRITERIA: dict[str, str] = {
    "reusable": "可复用",
    "actionable": "具体可操作",
    "evidenced": "有依据或证据",
    "correct": "正确不冲突",
    "novel": "有新增价值",
    "compliant": "合规",
}
VERDICTS: dict[str, str] = {"include": "收录", "merge": "合并为印证", "return": "退回补充", "reject": "不收"}
VERDICT_OF_LABEL = {v: k for k, v in VERDICTS.items()}
RANK = {"include": 0, "merge": 1, "return": 2, "reject": 3}

# 类型 → (标签, [(键, 必须回答的问题)])
QUESTIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "practice": ("公司做法", [
        ("scene", "在什么场景、什么条件下用（产品、行业、阶段）"),
        ("steps", "具体怎么做（步骤或要点，照着能做）"),
        ("evidence", "依据或证据是什么（依据条款，或在几个项目里用过、效果如何）"),
        ("limits", "什么情况下不适用、有什么风险"),
    ]),
    "pitfall": ("常见坑", [
        ("symptom", "现象：坑长什么样、在哪个环节出现"),
        ("cause", "原因：为什么会踩"),
        ("impact", "后果：踩了会怎样"),
        ("avoid", "规避做法：怎样提前发现、怎样避开"),
    ]),
    "value": ("经验数值", [
        ("range", "数值或区间是多少（只出区间，不出单个项目的值）"),
        ("basis", "口径：怎么算、单位是什么"),
        ("sample", "样本：来自几个项目（不少于 5 个）、什么时间"),
        ("scope", "适用范围：哪类客户、多大规模可参照"),
    ]),
    "template": ("方法模板", [
        ("purpose", "用途：解决什么问题、用在交付哪个阶段"),
        ("inputs", "输入：开始前要拿到什么"),
        ("structure", "步骤或结构：分几步、每步产出什么"),
        ("cautions", "注意事项：常见偏差和把关点"),
    ]),
    "golden": ("黄金样例", [
        ("slot", "范本对应哪个产品 × 行业的哪一章"),
        ("why", "推荐理由：好在哪里、可以学什么"),
        ("status", "是否已签署终版、客户验收（AI 基线写明“AI 基线，虚拟客户”）"),
        ("deid", "去标识后是否仍可读、不可再识别客户"),
    ]),
}
TEMPLATE_TYPES = ("TPL", "MTD")


def kind_of(doc_type: str, knowledge_kind: str = "") -> str:
    """条目属于哪类固定问题；法规原文切片、解读、映射等没有固定问题，返回空串（6 条标准照样适用）。"""
    if knowledge_kind == "golden" or doc_type == "GLD":
        return "golden"
    if knowledge_kind == "practice":
        return "practice"
    if doc_type == "PIT":
        return "pitfall"
    if doc_type == "BMK":
        return "value"
    if doc_type in TEMPLATE_TYPES:
        return "template"
    return ""


def question_keys(kind: str) -> list[str]:
    return [k for k, _ in QUESTIONS[kind][1]] if kind in QUESTIONS else []


def prompt_block(doc_type: str, knowledge_kind: str = "") -> str:
    """附在待审材料后面：本条按类型必须回答的问题。"""
    kind = kind_of(doc_type, knowledge_kind)
    if not kind:
        return "# 按类型必须回答的问题\n（本类型没有固定问题，answers 写 {}）"
    label, qs = QUESTIONS[kind]
    lines = [f"- {k}：{q}" for k, q in qs]
    return f"# 按类型必须回答的问题（{label}）\n草稿逐题答到了写 true，没答或答得不具体写 false：\n" + "\n".join(lines)


OUTPUT_FIELDS = """
另外给出收录判定（四期 B43）：
"criteria": {"reusable": {"pass": true, "reason": "一句话"}, "actionable": {...}, "evidenced": {...}, "correct": {...},
             "novel": {...}, "compliant": {...}},
"answers": {"按类型必须回答的问题的键": true},
"verdict": "收录 | 合并为印证 | 退回补充 | 不收", "merge_with": "合并为印证时写对方 KB 编号，否则空串"
6 条标准必须都给出；"按类型必须回答的问题"一节列了几题，answers 就给几个键。"""


@dataclass
class Decision:
    verdict: str                                        # include / merge / return / reject
    reasons: list[str] = field(default_factory=list)    # 没过的标准、没答的问题、程序规则（只键名对应的标签）
    given: bool = True                                  # Agent 给全了 6 条标准、类型问题和判定
    agent_verdict: str = ""

    @property
    def label(self) -> str:
        return VERDICTS[self.verdict]

    def to_json(self) -> dict:
        return {"verdict": self.verdict, "label": self.label, "reasons": self.reasons, "given": self.given,
                "agent_verdict": self.agent_verdict}


def clean(data: dict, doc_type: str, knowledge_kind: str = "") -> dict:
    """从 Agent 返回的 JSON 里取出收录标准、类型问题和判定；缺的留空，由 decide 处理。"""
    raw = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    criteria = {}
    for k in CRITERIA:
        c = raw.get(k)
        if isinstance(c, dict):
            criteria[k] = {"pass": c.get("pass") is True, "reason": str(c.get("reason") or "没有给出")[:300]}
    ans_raw = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    answers = {k: ans_raw.get(k) is True for k in question_keys(kind_of(doc_type, knowledge_kind)) if k in ans_raw}
    verdict = VERDICT_OF_LABEL.get(str(data.get("verdict") or "").strip(), "")
    merge_with = str(data.get("merge_with") or "")[:40]
    return {"criteria": criteria, "answers": answers, "verdict": verdict, "merge_with": merge_with}


def decide(intake: dict, doc_type: str, knowledge_kind: str = "", *, rules_failed: list[str] | None = None,
           checks_passed: bool = True, live: bool = False) -> Decision:
    """程序下限 + Agent 判定取更严者。live 时 Agent 漏答（标准、类型问题、判定任一缺）不算“收录”。"""
    criteria, answers = intake.get("criteria", {}), intake.get("answers", {})
    keys = question_keys(kind_of(doc_type, knowledge_kind))
    given = len(criteria) == len(CRITERIA) and set(keys) <= set(answers) and bool(intake.get("verdict"))
    floor, reasons = "include", []

    def lift(v: str, why: str) -> None:
        nonlocal floor
        reasons.append(why)
        if RANK[v] > RANK[floor]:
            floor = v

    if rules_failed:
        lift("return", "程序规则未过")
    if not checks_passed:
        lift("return", "审核清单未全部通过")
    label = QUESTIONS[kind_of(doc_type, knowledge_kind)][1] if keys else []
    for k, q in label:
        if k in answers and not answers[k]:
            lift("return", f"没答：{q.split('（')[0]}")
    for k, c in criteria.items():
        if c["pass"]:
            continue
        lift({"reusable": "reject", "novel": "merge"}.get(k, "return"), f"不符合：{CRITERIA[k]}")
    if live and not given:
        lift("return", "Agent 没有给全收录标准、类型问题或判定")
    agent = intake.get("verdict", "")
    final = agent if agent and RANK[agent] > RANK[floor] else floor
    if agent and RANK[agent] > RANK[floor]:
        reasons.append(f"Agent 判定：{VERDICTS[agent]}")
    return Decision(final, reasons, given, agent)


def kind_label(doc_type: str, knowledge_kind: str = "") -> str:
    k = kind_of(doc_type, knowledge_kind)
    return QUESTIONS[k][0] if k else meta.DOC_TYPES.get(doc_type, doc_type)
