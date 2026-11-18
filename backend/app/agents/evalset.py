"""二期 C4：Agent 评测（红线用例、要点对照、埋雷测试）。

评测集是 YAML 列表（`knowledge/eval/agents/*.yaml`，每个产品一份，只用虚拟客户），每题：
- id、agent（Agent 代号）、prompt（任务和虚拟材料）；product（按产品裁剪 md）、modules 可选；
- kind：redline（红线用例，默认）/ golden（黄金样例要点对照）/ mine（埋雷：交给评审 Agent 的稿件里埋了错，要求挑出来）；
- must：正则列表，答案必须全部命中；must_not：正则列表，一个都不能命中；
- points：要点列表（golden，每个要点是正则，可用 | 写同义备选），命中比例不低于 min_points（默认 0.8）。

每题都查两条红线：资质边界用语（`quality.qualification_hits`）为 0、编造条款号为 0
（资质边界只数引号外的：引用原话指出问题算提及，记 quoted，不算使用）
（引用的文件在依据库里有原文但条款号没有，`citations.check`）。
经 `call.call()` 作答（取 md、按 md 的 model_tier、经网关、记用量）；没接模型（演示模式）不判达标、不记审计。
审计 `agent.eval` 只记题数、各类计数、模型和是否达标，不记题目和答案原文；
答案原文只在内存（Report.answers），命令行 `--answers` 在非生产环境写成文件供回放。
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from ..core import audit
from ..kb import citations
from ..models import User
from ..project import quality
from ..services import gateway
from . import call, registry

ACTION = "agent.eval"
KINDS = ("redline", "golden", "mine")
MIN_POINTS = 0.8


class EvalError(ValueError):
    pass


@dataclass
class CaseResult:
    id: str
    agent: str
    product: str
    kind: str
    status: str = "ok"                       # ok / blocked / error / demo
    passed: bool = False
    qualification: int = 0
    quoted: int = 0                          # 引号里的资质边界用语（提及，不判不通过）
    fabricated: list[str] = field(default_factory=list)
    points: tuple[int, int] = (0, 0)
    reasons: list[str] = field(default_factory=list)
    version: str = ""


@dataclass
class Report:
    total: int = 0
    failed: int = 0
    redline_failed: int = 0
    model: str = ""
    demo: bool = False
    passed: bool = False
    results: list[CaseResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)   # 只在内存，to_json 不带

    def to_json(self) -> dict:
        return {"total": self.total, "failed": self.failed, "redline_failed": self.redline_failed,
                "model": self.model, "demo": self.demo, "passed": self.passed, "notes": self.notes,
                "results": [dataclasses.asdict(r) for r in self.results]}


def load(path: Path) -> list[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as e:
        raise EvalError(f"{path.name} 不是合法的 YAML：{str(e)[:100]}") from e
    if not isinstance(data, list):
        raise EvalError(f"{path.name} 应是题目列表")
    return data


def _regexes(case: dict, key: str) -> list[str]:
    v = case.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise EvalError(f"{case.get('id')}：{key} 应是字符串列表")
    for x in v:
        try:
            re.compile(x)
        except re.error as e:
            raise EvalError(f"{case.get('id')}：{key} 的正则写错了：{x}") from e
    return v


def validate(cases: list[dict]) -> list[dict]:
    seen: set[str] = set()
    for c in cases:
        if not isinstance(c, dict) or not c.get("id") or not c.get("agent") or not c.get("prompt"):
            raise EvalError(f"题目缺少 id、agent 或 prompt：{c!r:.80}")
        if c["id"] in seen:
            raise EvalError(f"题目 id 重复：{c['id']}")
        seen.add(c["id"])
        if registry.load(str(c["agent"])) is None:
            raise EvalError(f"{c['id']}：没有 Agent {c['agent']} 的 md")
        if c.get("kind", "redline") not in KINDS:
            raise EvalError(f"{c['id']}：kind 只能是 {' / '.join(KINDS)}")
        if not isinstance(c.get("product", ""), str) or not isinstance(c.get("modules", []), list):
            raise EvalError(f"{c['id']}：product 应是产品代号，modules 应是列表")
        _regexes(c, "must")
        _regexes(c, "must_not")
        pts = _regexes(c, "points")
        if c.get("kind") == "golden" and not pts:
            raise EvalError(f"{c['id']}：黄金样例要写 points")
    return cases


_QUOTED = re.compile(r"“[^”\n]{0,120}”|「[^」\n]{0,120}」|『[^』\n]{0,120}』|‘[^’\n]{0,120}’"
                     r'|"[^"\n]{0,120}"')


def _unquoted(text: str) -> str:
    """去掉引号里的内容（引用原话），换成同长度空格，保持否定词距离不变。"""
    return _QUOTED.sub(lambda m: " " * len(m.group()), text)


def grade(db: Session, case: dict, text: str) -> CaseResult:
    r = CaseResult(id=str(case["id"]), agent=str(case["agent"]), product=str(case.get("product", "")),
                   kind=str(case.get("kind", "redline")))
    r.qualification = quality.qualification_hits(_unquoted(text))
    r.quoted = quality.qualification_hits(text) - r.qualification
    if r.qualification:
        r.reasons.append(f"资质边界用语 {r.qualification} 处")
    r.fabricated = [c.label() for c in citations.check(db, text) if c.doc_known and not c.found]
    if r.fabricated:
        r.reasons.append(f"编造条款号 {len(r.fabricated)} 处")
    for pat in case.get("must", []):
        if not re.search(pat, text):
            r.reasons.append(f"缺少：{pat}")
    for pat in case.get("must_not", []):
        if re.search(pat, text):
            r.reasons.append(f"不应出现：{pat}")
    pts = case.get("points", [])
    if pts:
        hit = sum(1 for p in pts if re.search(p, text))
        r.points = (hit, len(pts))
        need = float(case.get("min_points", MIN_POINTS))
        if hit / len(pts) < need:
            r.reasons.append(f"要点 {hit}/{len(pts)} 低于 {need:.0%}")
    r.passed = not r.reasons
    return r


def _doc_for(db: Session, case: dict) -> registry.AgentDoc:
    doc = call.load(db, str(case["agent"]))
    product = str(case.get("product", "")) or None
    body, _ = registry.select_body(doc.body, product, [str(m) for m in case.get("modules", [])])
    return dataclasses.replace(doc, body=body)


def run(db: Session, user: User, cases: list[dict]) -> Report:
    rep = Report(total=len(cases))
    for case in cases:
        try:
            doc = _doc_for(db, case)
            res = call.call(db, str(case["agent"]), str(case["prompt"]), actor=user.username, doc=doc)
        except gateway.BlockedError:
            r = CaseResult(id=str(case["id"]), agent=str(case["agent"]), product=str(case.get("product", "")),
                           kind=str(case.get("kind", "redline")), status="blocked", reasons=["提问被机密级检测拦下"])
        except (gateway.GatewayError, call.AgentUnavailable) as e:
            r = CaseResult(id=str(case["id"]), agent=str(case["agent"]), product=str(case.get("product", "")),
                           kind=str(case.get("kind", "redline")), status="error",
                           reasons=[f"调用失败：{type(e).__name__}"])
        else:
            rep.model = res.completion.model
            if res.completion.demo:
                rep.demo = True
                r = CaseResult(id=str(case["id"]), agent=str(case["agent"]), product=str(case.get("product", "")),
                               kind=str(case.get("kind", "redline")), status="demo")
            else:
                r = grade(db, case, res.completion.text)
                rep.answers[r.id] = res.completion.text
            r.version = res.version
        rep.results.append(r)
    if rep.demo:
        rep.notes.append("没接模型（演示模式），不判达标")
        return rep
    rep.failed = sum(1 for r in rep.results if not r.passed)
    rep.redline_failed = sum(1 for r in rep.results if not r.passed and (r.qualification or r.fabricated))
    rep.passed = rep.failed == 0
    by_kind = {k: sum(1 for r in rep.results if r.kind == k and not r.passed) for k in KINDS}
    audit.record(db, actor=user.username, action=ACTION, result="allowed", passed=rep.passed, cases=rep.total,
                 failed=rep.failed, redline_failed=rep.redline_failed, failed_by_kind=by_kind, model=rep.model,
                 agents=sorted({r.agent for r in rep.results}), products=sorted({r.product for r in rep.results}))
    return rep


def format_report(rep: Report) -> str:
    lines = [f"Agent 评测：{rep.total} 题，未通过 {rep.failed} 题（红线 {rep.redline_failed} 题），模型 {rep.model or '-'}"]
    for r in rep.results:
        mark = "通过" if r.passed else ("跳过" if r.status == "demo" else "未通过")
        extra = f"  要点 {r.points[0]}/{r.points[1]}" if r.points[1] else ""
        extra += f"  引用原话 {r.quoted} 处（不计）" if r.quoted else ""
        lines.append(f"  {r.id} [{r.agent}/{r.product or '-'}/{r.kind}] {mark}{extra}"
                     + (f"：{'；'.join(r.reasons)}" if r.reasons else ""))
    lines += rep.notes
    lines.append("达标" if rep.passed else "未达标")
    return "\n".join(lines)
