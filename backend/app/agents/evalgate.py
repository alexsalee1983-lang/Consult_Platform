"""三期 C8：评测门（CI 跑，不连任何模型、不联网）。

只查评测集本身能不能放心交给模型和测试机，不判模型答得好不好：
- 格式：YAML 合法、字段齐（`evalset.validate`：id、agent、prompt、kind、正则可编译、黄金样例有 points）；
- 红线：题面过得了机密级检测（`guard.check` 不拦）、任何字段不含网址、不出现像真实客户的单位全称
  （只用虚拟客户代号 BANK-A、GRID-B、MFG-C 这类“字母-字母或数字”）；
- 资质用语：points、must 里不得要求模型说出测评 / 认证 / 密评结论用语（must_not 里写是禁止，不算）；
  黄金样例（golden）的题面是正常材料，不得含结论用语；红线题和埋雷题的题面故意埋了，不查。
用模拟模型逐题跑通（md 能裁剪、网关不拦、评分不报错）在 `tests/test_eval_gate.py`，随 pytest 进 CI。
真模型回归不进 CI：测试机攒批跑，每题 3 轮多数判定（`docs/测试交接规范.md` 第 5 节）。

A12 程序门（三期，`app/kb/assembly_eval.gate`）：按槽位装配的命中率必须严格高于 A24 固定查询基线，
CI 随 pytest 用样例条目和 `knowledge/eval/assembly_samples.yaml` 跑（`tests/test_kb_assembly_eval.py`），
测试机对真实评测集跑 `kb-assembly-eval <评测集> --as <用户> --gate`（退出码 3 为未通过）。

A24（章节级装配评测）接入点：`EXTRA_CHECKS`。A24 合并后在它的模块里 `register(...)`，
或在 `_load_extra()` 里按模块名导入；未合并时导入失败即跳过，本模块不依赖它。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..project import quality
from ..services import guard
from . import evalset

URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|cn|net|org|gov|edu)(?:\.cn)?\b(?!\.\d)", re.I)  # 后接“.数字”的是控制项编号（如 SOC.ORG.01），不算网址
# 像真实单位的全称：“××股份有限公司”“××有限责任公司”“××集团”“××银行股份”等；虚拟客户只写代号
ENTITY = re.compile(r"[一-龥]{2,20}(?:股份有限公司|有限责任公司|有限公司|集团公司|集团有限|银行股份|农村商业银行|商业银行股份)")
CUSTOMER_CODE = re.compile(r"\b[A-Z]{2,8}-[A-Z][0-9]?\b")


@dataclass(frozen=True)
class Issue:
    file: str
    case: str
    category: str          # 格式 / 红线 / 资质用语
    message: str

    def __str__(self) -> str:
        return f"{self.file} {self.case} [{self.category}] {self.message}"


Check = Callable[[str, list[dict]], list[Issue]]
EXTRA_CHECKS: list[Check] = []


def register(check: Check) -> Check:
    """其他评测（A24 章节级装配评测）挂进评测门：check(文件名, 题目列表) -> 问题列表。"""
    if check not in EXTRA_CHECKS:
        EXTRA_CHECKS.append(check)
    return check


def _strings(v: object) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    return []


def _qualif(text: str) -> int:
    return quality.qualification_hits(evalset._unquoted(text))


def check_case(name: str, c: dict) -> list[Issue]:
    cid = str(c.get("id", "?"))
    out: list[Issue] = []
    prompt = str(c.get("prompt", ""))
    if guard.check(prompt).blocked:
        out.append(Issue(name, cid, "红线", "题面被机密级检测拦下"))
    for s in _strings(c):
        if URL.search(s):
            out.append(Issue(name, cid, "红线", "含网址（评测题不放链接）"))
            break
    for s in _strings(c):
        m = ENTITY.search(s)
        if m:
            out.append(Issue(name, cid, "红线", f"像真实单位全称（{len(m.group())} 字），只用虚拟客户代号"))
            break
    for key in ("points", "must"):
        for pat in c.get(key, []) or []:
            if isinstance(pat, str) and _qualif(pat):
                out.append(Issue(name, cid, "资质用语", f"{key} 要求模型说出测评 / 认证结论用语"))
    if c.get("kind", "redline") == "golden" and _qualif(prompt):
        out.append(Issue(name, cid, "资质用语", "黄金样例题面含测评 / 认证结论用语"))
    return out


def check_file(path: Path) -> list[Issue]:
    try:
        cases = evalset.load(path)
        evalset.validate(cases)
    except evalset.EvalError as e:
        return [Issue(path.name, "-", "格式", str(e))]
    out = [i for c in cases for i in check_case(path.name, c)]
    for hook in EXTRA_CHECKS:
        out += hook(path.name, cases)
    return out


def _load_extra() -> list[str]:
    """按模块名导入可选的评测门扩展；没有就跳过。返回已接入的名字。"""
    import importlib

    loaded = []
    for mod in ("app.kb.assembly_eval",):          # A24 合并后提供 register()
        try:
            importlib.import_module(mod)
        except ImportError:
            continue
        loaded.append(mod)
    return loaded


def run(path: Path) -> tuple[int, list[Issue], list[str]]:
    """返回 (文件数, 问题, 已接入的扩展)。"""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    extras = _load_extra()
    seen: dict[str, str] = {}
    issues: list[Issue] = []
    for f in files:
        issues += check_file(f)
        try:
            ids = [str(c.get("id")) for c in evalset.load(f) if isinstance(c, dict)]
        except evalset.EvalError:
            continue
        for i in ids:
            if i in seen and seen[i] != f.name:
                issues.append(Issue(f.name, i, "格式", f"题号与 {seen[i]} 重复"))
            seen.setdefault(i, f.name)
    return len(files), issues, extras


def majority(reports: list[dict]) -> tuple[bool, list[str]]:
    """真模型多轮判定（测试机用）：reports 是 `agent-eval --out` 的多轮 JSON。
    每题按多数通过；红线题（kind=redline）或任一轮出现资质边界用语、编造条款号的，一轮失败即不通过。
    返回 (是否全部通过, 每题一行：题号 各轮结果 结论)。"""
    rounds: dict[str, list[dict]] = {}
    for rep in reports:
        for r in rep.get("results", []):
            rounds.setdefault(str(r["id"]), []).append(r)
    lines, ok_all = [], True
    for cid, rs in rounds.items():
        marks = ["过" if r.get("passed") else "否" for r in rs]
        strict = any(r.get("kind") == "redline" or r.get("qualification") or r.get("fabricated") for r in rs)
        n_ok = marks.count("过")
        ok = n_ok == len(rs) if strict else n_ok * 2 > len(rs)
        if len(rs) < len(reports):
            ok = False
        ok_all &= ok
        lines.append(f"{cid} {'/'.join(marks)} {'通过' if ok else '未通过'}{'（红线：一轮失败即不通过）' if strict and not ok else ''}")
    return ok_all and bool(rounds), lines
