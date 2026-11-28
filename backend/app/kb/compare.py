"""有无知识库对比评测（二期 B1 补充，python -m app.cli kb-eval <题目文件> --as <用户> --compare）。

同一批题目分别“不用知识库”和“用知识库”（先按题检索，把命中的条目片段交给模型）让模型作答，都经网关
gateway.complete（外部模型只用于虚拟数据，网关照常假名化、机密级检测）。平台自动核对答案里写的“文件 + 条款号”
（识别规则同 B8 起草引用校验 citations.find）：

- 准确：答案引用了题目 expect_refs 里的依据之一（文件名或标准号 + 条款号都对上；标准号可不带年份）。
- 编造条号：答案引用的文件在库里有依据原文（SRC），但库里没有这一条 / 这个条款号。文件本身不在库里的只计“库外引用”，
  无法判断真假，不算编造。

题目文件同 kb-eval，对比模式只用带 expect_refs 的题：

    - id: C001
      category: clause            # clause 按条号查询 / mapping 跨依据映射 / industry 行业要求
      question: 网络运营者对网络日志要留存多久？
      expect_refs: ["《网络安全法》第二十三条"]      # 网安法按 2025 修正后的条号
      product: SOC                # 可选

达标：用知识库的准确率 ≥ KB_EVAL_MIN_ACCURACY、编造条号 0 次，且不低于不用知识库的准确率。
结果写审计 kb.eval_compare（只记题数、准确率、编造次数和是否达标，不记题目和答案原文）。KB_EVAL_GATE 开启时，
最近一次对比评测未达标会暂停自动发布（autopublish.suspended，B10 / B11 两级都停），新入库的转人工审核，
直到再跑一次达标；所以每批入库后跑一次（kb-source-publish / kb-import --submit 之后）。
没接模型（演示模式）时只出报告、不记达标与否。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import AuditLog, User
from ..services import gateway, usage
from . import citations, search

LEVEL = "standard"
TOP_K = 5
CATEGORIES = {"clause": "按条号查询", "mapping": "跨依据映射", "industry": "行业要求"}
ACTION = "kb.eval_compare"
SNIPPET = 400

SYSTEM = ("你在参加安全合规依据问答评测。回答问题，并写明依据：法规写《文件名》第X条，标准写标准号和条款号"
          "（例如 GB/T 22239-2019 8.1.4.1）。拿不准的依据写“依据待核实”，不要编造文件名或条款号。只做差距分析、"
          "符合性初判和整改建议，不出具测评、认证结论。回答控制在 300 字以内。")
SYSTEM_KB = SYSTEM + "只能根据用户消息里给出的知识库条目作答；条目里没有的依据写“依据待核实”。"


class CompareError(ValueError):
    pass


@dataclass
class Answer:
    correct: bool = False
    cited: list[str] = field(default_factory=list)       # 答案里识别出的引用（文件 + 条款号）
    fabricated: list[str] = field(default_factory=list)  # 文件在库里、条款号库里没有
    outside: int = 0                                     # 文件不在库里的引用数
    status: str = "ok"                                   # ok / blocked / error / demo


@dataclass
class CompareResult:
    id: str
    category: str
    expect_refs: list[str]
    no_kb: Answer
    with_kb: Answer
    retrieved: int = 0


@dataclass
class CompareReport:
    total: int = 0
    model: str = ""
    demo: bool = False
    no_kb: dict = field(default_factory=dict)
    with_kb: dict = field(default_factory=dict)
    by_category: dict = field(default_factory=dict)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    results: list[CompareResult] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["results"] = [asdict(r) for r in self.results]
        return d


def select_questions(questions: list[dict]) -> list[dict]:
    out = []
    for q in questions:
        refs = q.get("expect_refs")
        if refs is None:
            continue
        if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x.strip() for x in refs):
            raise CompareError(f"{q['id']}：expect_refs 应是非空的依据列表，例如 [\"《网络安全法》第二十三条\"]")
        if not all(citations.find(x) for x in refs):
            raise CompareError(f"{q['id']}：expect_refs 里有认不出“文件 + 条款号”的写法")
        cat = q.get("category", "clause")
        if cat not in CATEGORIES:
            raise CompareError(f"{q['id']}：category 只能是 {' / '.join(CATEGORIES)}")
        out.append(q)
    if not out:
        raise CompareError("没有带 expect_refs 的题，对比模式没有可评的题")
    return out


def _key(doc: str, part: str) -> tuple[str, str]:
    if citations._is_code(doc):
        return re.sub(r"-\d{4}$", "", citations._norm_code(doc)), part
    return citations._norm_doc(doc), part


def _expected(refs: list[str]) -> set[tuple[str, str]]:
    return {_key(d, p) for r in refs for d, _, p in citations.find(r)}


def grade(db: Session, text: str, expect: set[tuple[str, str]]) -> Answer:
    a = Answer()
    for c in citations.check(db, text):
        label = c.label()
        a.cited.append(label)
        if c.found:
            continue
        if c.doc_known:
            a.fabricated.append(label)
        else:
            a.outside += 1
    a.correct = any(_key(d, p) in expect for d, _, p in citations.find(text))
    return a


def _context(db: Session, user: User, q: dict) -> tuple[str, int]:
    product = str(q.get("product", "")).strip()
    hits = search.basis(db, user, q["question"], limit=TOP_K, product=product)["items"]
    if q.get("category", "clause") != "clause":
        seen = {h["doc_id"] for h in hits}
        hits += [h for h in search.search(db, user, q["question"], product=product, limit=TOP_K)["items"]
                 if h["doc_id"] not in seen]
    lines = []
    for i, h in enumerate(hits, 1):
        ref = f"（依据：{h['ref_document']} {h['ref_version']}）" if h.get("ref_document") else ""
        lines.append(f"[{i}] {h['title']}{ref}\n{(h.get('snippet') or '')[:SNIPPET]}")
    return "\n\n".join(lines) or "（知识库没有检索到相关条目）", len(hits)


def _ask(db: Session, user: User, message: str, system: str) -> tuple[str, str, str]:
    """返回 (答案, 状态, 模型)。"""
    try:
        with gateway.lane("kb"):
            out = gateway.complete(message, tier="standard", project_code="", level=LEVEL, system=system)
    except gateway.BlockedError:
        return "", "blocked", gateway.model_for(LEVEL)
    except gateway.GatewayError as e:
        usage.record(db, username=user.username, project_code="", level=LEVEL, model=gateway.model_for(LEVEL),
                     status="error", stream=False, attempts=e.attempts)
        return "", "error", gateway.model_for(LEVEL)
    usage.record(db, username=user.username, project_code="", level=LEVEL, model=out.model, status="ok",
                 stream=False, completion=out)
    return out.text, "demo" if out.demo else "ok", out.model


def _summary(answers: list[Answer]) -> dict:
    n = len(answers)
    return {"questions": n, "accuracy": round(sum(a.correct for a in answers) / n, 4) if n else 0.0,
            "fabricated": sum(len(a.fabricated) for a in answers), "outside": sum(a.outside for a in answers),
            "failed_calls": sum(a.status in ("blocked", "error") for a in answers)}


def run(db: Session, user: User, questions: list[dict]) -> CompareReport:
    qs = select_questions(questions)
    report = CompareReport(total=len(qs))
    for q in qs:
        expect = _expected(q["expect_refs"])
        bare, s1, report.model = _ask(db, user, q["question"], SYSTEM)
        ctx, n = _context(db, user, q)
        kb, s2, _ = _ask(db, user, f"知识库条目：\n{ctx}\n\n问题：{q['question']}", SYSTEM_KB)
        if "demo" in (s1, s2):
            report.demo = True
        a, b = grade(db, bare, expect), grade(db, kb, expect)
        a.status, b.status = s1, s2
        report.results.append(CompareResult(q["id"], q.get("category", "clause"), list(q["expect_refs"]), a, b, n))
    report.no_kb = _summary([r.no_kb for r in report.results])
    report.with_kb = _summary([r.with_kb for r in report.results])
    for cat in CATEGORIES:
        rs = [r for r in report.results if r.category == cat]
        if rs:
            report.by_category[cat] = {"no_kb": _summary([r.no_kb for r in rs]),
                                       "with_kb": _summary([r.with_kb for r in rs])}
    floor = get_settings().kb_eval_min_accuracy
    if report.with_kb["accuracy"] < floor:
        report.reasons.append(f"用知识库的准确率 {report.with_kb['accuracy']:.0%} 低于 {floor:.0%}")
    if report.with_kb["fabricated"]:
        report.reasons.append(f"用知识库仍编造条号 {report.with_kb['fabricated']} 次")
    if report.with_kb["accuracy"] < report.no_kb["accuracy"]:
        report.reasons.append("用知识库的准确率低于不用知识库")
    if report.with_kb["failed_calls"]:
        report.reasons.append(f"{report.with_kb['failed_calls']} 题调用模型失败或被拦")
    report.passed = not report.reasons and not report.demo
    if report.demo:
        report.reasons.append("没接模型（演示模式），不判达标")
    else:
        audit.record(db, actor=user.username, action=ACTION, result="allowed", passed=report.passed,
                     questions=report.total, model=report.model,
                     no_kb_accuracy=report.no_kb["accuracy"], with_kb_accuracy=report.with_kb["accuracy"],
                     no_kb_fabricated=report.no_kb["fabricated"], with_kb_fabricated=report.with_kb["fabricated"])
    return report


def gate_reason(db: Session) -> str:
    """最近一次对比评测未达标时返回暂停原因；没跑过或已达标返回空串。"""
    if not get_settings().kb_eval_gate:
        return ""
    d = db.scalar(select(AuditLog.detail).where(AuditLog.action == ACTION, AuditLog.result == "allowed")
                  .order_by(AuditLog.id.desc()).limit(1))
    if not d:
        return ""
    try:
        x = json.loads(d)
    except ValueError:
        return ""
    if x.get("passed"):
        return ""
    return (f"最近一次有无知识库对比评测未达标（用知识库准确率 {x.get('with_kb_accuracy', 0):.0%}、"
            f"编造条号 {x.get('with_kb_fabricated', 0)} 次），再跑一次 kb-eval --compare 达标后恢复")


def format_report(r: CompareReport) -> str:
    def line(name: str, s: dict) -> str:
        return (f"{name}：准确率 {s['accuracy']:.0%}，编造条号 {s['fabricated']} 次，库外引用 {s['outside']} 次"
                + (f"，调用失败 {s['failed_calls']} 题" if s["failed_calls"] else ""))

    lines = [f"有无知识库对比评测：{r.total} 题（模型 {r.model or '—'}）",
             line("不用知识库", r.no_kb), line("用知识库", r.with_kb), ""]
    for cat, m in r.by_category.items():
        lines.append(f"{CATEGORIES[cat]}：{m['with_kb']['questions']} 题，准确率 不用 {m['no_kb']['accuracy']:.0%}"
                     f" → 用 {m['with_kb']['accuracy']:.0%}，编造条号 {m['no_kb']['fabricated']} → "
                     f"{m['with_kb']['fabricated']}")
    lines += ["", "结论：" + ("达标" if r.passed else "未达标：" + "；".join(r.reasons))]
    bad = [x for x in r.results if not x.with_kb.correct or x.with_kb.fabricated]
    if bad:
        lines += ["", f"用知识库仍答错或编造的题（{len(bad)}）："]
        lines += [f"  {x.id}（{CATEGORIES[x.category]}）期望 {x.expect_refs}，答案引用 {x.with_kb.cited[:3] or '无'}"
                  + (f"，编造 {x.with_kb.fabricated}" if x.with_kb.fabricated else "") for x in bad]
    return "\n".join(lines)
