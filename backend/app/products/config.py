"""服务产品与模块配置：读取和校验（二期 A1，格式见 docs/接口约定.md 第二、三节）。

配置放在仓库根目录的 products/*.yaml、modules/*.yaml（镜像内 /srv/products、/srv/modules），与 Agent md 一样版本化，
改配置走 PR、负责人审批（决策记录 2026-11-14）。本模块只读文件、不连数据库；立项快照（A3）和交付链（A4）在此之上。

规则：
1. 校验只报问题清单，不抛到一半；`validate()` 返回空列表才算通过（CI：tests/test_product_config.py）。
2. 门禁规则只能从 GATE_KINDS 里选类型、填参数，配置里不能写代码。
3. 提示词模板只允许 TEMPLATE_VARS 里的变量，平台固定的输出规则、严格档规则、资质边界说明不进配置。
4. 配置里的文字都要过机密级检测（`guard.check`）：配置进仓库，不得含口令、IP 等机密级内容，也不写客户信息。
5. 配置版本 = 解析后内容规范化 JSON 的 sha256 前 12 位（改注释、空行不改版本）。
"""
from __future__ import annotations

import hashlib
import json
import re
import string
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.config import get_settings
from ..kb import meta
from ..services import guard

PRODUCT_CODE = re.compile(r"^[A-Z]{2,6}$")
MODULE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,7}$")
DELIVERABLE_CODE = re.compile(r"^D([1-9][0-9]?)$")
STEP_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
GATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
STATUSES = ("ready", "draft")
STAGES = tuple(s for s in meta.STAGES if s != "通用")
LEVELS = ("blocking", "major")
TEMPLATE_VARS = frozenset({"product_name", "domains", "frameworks", "basis", "number", "modules",
                           "module_instructions"})
GATE_KINDS = ("coverage", "required_sections", "forbidden_terms", "min_sources")
RULES_DIR = "rules"


@dataclass(frozen=True)
class Deliverable:
    code: str
    title: str
    stage: str
    client_facing: bool
    outline: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    key: str
    number: int
    name: str
    agent: str
    inputs: tuple[str, ...]
    output: str                 # 空 = 产出存运行记录
    query: str
    instruction: str


@dataclass(frozen=True)
class Gate:
    id: str
    kind: str
    deliverable: str            # forbidden_terms 可为空（全部交付件）
    level: str
    message: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Product:
    code: str
    name: str
    domain: str
    status: str
    modules: tuple[str, ...]
    deliverables: tuple[Deliverable, ...]
    domains: tuple[str, ...]
    frameworks: tuple[str, ...]
    basis: tuple[str, ...]
    chain: tuple[Step, ...]
    gates: tuple[Gate, ...]
    card_items: tuple[str, ...]
    version: str
    raw: dict[str, Any]

    def deliverable(self, code: str) -> Deliverable | None:
        return next((d for d in self.deliverables if d.code == code), None)


@dataclass(frozen=True)
class Module:
    code: str
    name: str
    status: str
    products: tuple[str, ...]
    sections: dict[str, dict[str, tuple[str, ...]]]
    frameworks: tuple[str, ...]
    basis: tuple[str, ...]
    instructions: dict[str, str]
    gates: tuple[Gate, ...]
    card_items: tuple[str, ...]
    kb_tags: tuple[str, ...]
    version: str
    raw: dict[str, Any]


@dataclass
class Catalog:
    products: dict[str, Product] = field(default_factory=dict)
    modules: dict[str, Module] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def catalog_dir() -> Path:
    """products/、modules/ 所在目录：配置项 catalog_dir；留空时依次找 /srv（镜像内）和仓库根目录。"""
    configured = get_settings().catalog_dir
    if configured:
        return Path(configured)
    for candidate in (Path("/srv"), Path(__file__).resolve().parents[3]):
        if (candidate / "products").is_dir():
            return candidate
    return Path("/srv")


def content_version(data: Any) -> str:
    canon = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


# ---- 小工具：取字段并记录问题 ----

class _P:
    def __init__(self, where: str, out: list[str]):
        self.where, self.out = where, out

    def add(self, msg: str) -> None:
        self.out.append(f"{self.where}：{msg}")

    def text(self, d: dict, key: str, required: bool = True, max_len: int = 200) -> str:
        v = d.get(key)
        if v is None or v == "":
            if required:
                self.add(f"缺少 {key}")
            return ""
        if not isinstance(v, str):
            self.add(f"{key} 应为文字")
            return ""
        v = v.strip()
        if len(v) > max_len:
            self.add(f"{key} 超过 {max_len} 字")
        return v

    def texts(self, d: dict, key: str, required: bool = False) -> tuple[str, ...]:
        v = d.get(key)
        if v is None:
            if required:
                self.add(f"缺少 {key}")
            return ()
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            self.add(f"{key} 应为非空文字的列表")
            return ()
        if len(set(v)) != len(v):
            self.add(f"{key} 有重复项")
        return tuple(x.strip() for x in v)


def _template_vars(tpl: str) -> set[str] | None:
    try:
        return {f for _, f, _, _ in string.Formatter().parse(tpl) if f is not None}
    except ValueError:
        return None


def _check_template(p: _P, label: str, tpl: str) -> None:
    names = _template_vars(tpl)
    if names is None:
        p.add(f"{label} 的花括号不成对（字面花括号写成 {{{{ }}}}）")
        return
    bad = sorted(n for n in names if n not in TEMPLATE_VARS)
    if bad:
        p.add(f"{label} 用了不允许的变量：{'、'.join(bad) or '空变量 {}'}")


def _gates(p: _P, raw: Any, base: Path) -> tuple[Gate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        p.add("gates 应为列表")
        return ()
    out, seen = [], set()
    for i, g in enumerate(raw):
        gp = _P(f"{p.where} gates[{i}]", p.out)
        if not isinstance(g, dict):
            gp.add("应为对象")
            continue
        gid = gp.text(g, "id")
        if gid and not GATE_ID.match(gid):
            gp.add(f"id {gid!r} 只能用小写字母、数字和连字符")
        if gid in seen:
            gp.add(f"id {gid} 重复")
        seen.add(gid)
        kind = gp.text(g, "kind")
        if kind and kind not in GATE_KINDS:
            gp.add(f"kind {kind!r} 不在 {'/'.join(GATE_KINDS)} 里")
        level = gp.text(g, "level")
        if level and level not in LEVELS:
            gp.add(f"level {level!r} 应为 blocking 或 major")
        message = gp.text(g, "message")
        dcode = gp.text(g, "deliverable", required=kind != "forbidden_terms")
        params: dict[str, Any] = {}
        if kind == "coverage":
            items = gp.texts(g, "items")
            fname = gp.text(g, "items_file", required=False)
            if bool(items) == bool(fname):
                gp.add("coverage 要写 items 或 items_file 其中一个")
            if fname:
                items = _items_file(gp, base, fname)
            params["items"] = list(items)
        elif kind == "required_sections":
            params["sections"] = list(gp.texts(g, "sections", required=True))
        elif kind == "forbidden_terms":
            params["terms"] = list(gp.texts(g, "terms", required=True))
        elif kind == "min_sources":
            m = g.get("min")
            if not isinstance(m, int) or isinstance(m, bool) or m < 1:
                gp.add("min_sources 的 min 应为正整数")
                m = 1
            params["min"] = m
        out.append(Gate(gid, kind, dcode, level, message, params))
    return tuple(out)


def _items_file(p: _P, base: Path, fname: str) -> tuple[str, ...]:
    rules = (base / RULES_DIR).resolve()
    path = (rules / fname).resolve()
    if rules not in path.parents:
        p.add(f"items_file 只能指向 {RULES_DIR}/ 下的文件")
        return ()
    if not path.is_file():
        p.add(f"items_file {fname} 不存在")
        return ()
    items = tuple(ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#"))
    if not items:
        p.add(f"items_file {fname} 是空的")
    return items


def _read_yaml(path: Path, out: list[str]) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        out.append(f"{path.name}：读不出来（{type(e).__name__}）")
        return None
    if not isinstance(data, dict):
        out.append(f"{path.name}：顶层应为对象")
        return None
    return data


def _strings(data: Any) -> list[str]:
    if isinstance(data, str):
        return [data]
    if isinstance(data, dict):
        return [s for v in data.values() for s in _strings(v)] + [k for k in data if isinstance(k, str)]
    if isinstance(data, list):
        return [s for v in data for s in _strings(v)]
    return []


def _guard(p: _P, data: dict) -> None:
    found: set[str] = set()
    for s in _strings(data):
        r = guard.check(s)
        if r.blocked:
            found |= {f.label for f in r.findings}
    kinds = sorted(found)
    if kinds:
        p.add(f"含机密级内容（{'、'.join(kinds)}），配置里不得出现")


def parse_product(path: Path, out: list[str], agents: set[str] | None = None) -> Product | None:
    data = _read_yaml(path, out)
    if data is None:
        return None
    p = _P(path.name, out)
    _guard(p, data)
    code = p.text(data, "code")
    if code and not PRODUCT_CODE.match(code):
        p.add(f"code {code!r} 应为 2～6 位大写字母")
    if code and path.stem != code.lower():
        p.add(f"文件名应为 {code.lower()}.yaml")
    if code == "GEN":
        p.add("GEN 是通用知识的元数据取值，不能作为产品")
    name = p.text(data, "name", max_len=64)
    domain = p.text(data, "domain")
    if domain and domain not in meta.DOMAINS:
        p.add(f"domain {domain!r} 不是知识库一级目录代号（{'/'.join(meta.DOMAINS)}）")
    status = p.text(data, "status")
    if status and status not in STATUSES:
        p.add(f"status {status!r} 应为 ready 或 draft")
    if status == "ready" and code and code not in meta.PRODUCTS:
        p.add(f"产品 {code} 还没登记到知识元数据（app/kb/meta.py PRODUCTS，同步公共规范第 7 节），不能设为 ready")
    modules = p.texts(data, "modules")

    deliverables: list[Deliverable] = []
    raw_d = data.get("deliverables")
    if not isinstance(raw_d, list) or not raw_d:
        p.add("deliverables 应为非空列表")
        raw_d = []
    for i, d in enumerate(raw_d):
        dp = _P(f"{path.name} deliverables[{i}]", out)
        if not isinstance(d, dict):
            dp.add("应为对象")
            continue
        dcode = dp.text(d, "code")
        m = DELIVERABLE_CODE.match(dcode) if dcode else None
        if dcode and (not m or int(m.group(1)) != i + 1):
            dp.add(f"编号应为 D{i + 1}（从 D1 起连续）")
        title = dp.text(d, "title", max_len=128)
        stage = dp.text(d, "stage", required=status == "ready")
        if stage and stage not in STAGES:
            dp.add(f"stage {stage!r} 应为 {'/'.join(STAGES)} 之一")
        cf = d.get("client_facing")
        if not isinstance(cf, bool):
            dp.add("client_facing 应为 true 或 false")
            cf = True
        outline = dp.texts(d, "outline")
        if any(len(t) > 60 for t in outline):
            dp.add("outline 的章节标题不超过 60 字")
        deliverables.append(Deliverable(dcode, title, stage, cf, outline))
    dcodes = {d.code for d in deliverables}

    base_raw = data.get("baseline") or {}
    if not isinstance(base_raw, dict):
        p.add("baseline 应为对象")
        base_raw = {}
    bp = _P(f"{path.name} baseline", out)
    domains = bp.texts(base_raw, "domains", required=status == "ready")
    frameworks = bp.texts(base_raw, "frameworks")
    basis = bp.texts(base_raw, "basis")

    chain: list[Step] = []
    raw_c = data.get("chain")
    if raw_c is None:
        raw_c = []
    if not isinstance(raw_c, list):
        p.add("chain 应为列表")
        raw_c = []
    if status == "ready" and not raw_c:
        p.add("ready 的产品至少要有一个交付链步骤")
    keys: list[str] = []
    for i, s in enumerate(raw_c):
        sp = _P(f"{path.name} chain[{i}]", out)
        if not isinstance(s, dict):
            sp.add("应为对象")
            continue
        key = sp.text(s, "key")
        if key and not STEP_KEY.match(key):
            sp.add(f"key {key!r} 只能用小写字母、数字和下划线，字母开头")
        if key in keys:
            sp.add(f"key {key} 重复")
        number = s.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 20:
            sp.add("number 应为 1～20 的整数")
            number = 0
        sname = sp.text(s, "name", max_len=40)
        agent = sp.text(s, "agent")
        if agent and agents is not None and agent not in agents:
            sp.add(f"agent {agent} 没有对应的 agents/roles/{agent}.md")
        inputs = sp.texts(s, "inputs", required=True)
        for inp in inputs:
            if inp == "evidence" or inp in dcodes:
                continue
            if inp.startswith("run:") and inp[4:] in keys:
                continue
            sp.add(f"inputs 里的 {inp!r} 应为 evidence、本产品的交付件编号或前面步骤的 run:<key>")
        output = s.get("output") or ""
        if not isinstance(output, str) or (output and output not in dcodes):
            sp.add(f"output {output!r} 不是本产品的交付件编号")
            output = ""
        query = sp.text(s, "query", max_len=200)
        instruction = sp.text(s, "instruction", max_len=4000)
        _check_template(sp, "query", query)
        _check_template(sp, "instruction", instruction)
        keys.append(key)
        chain.append(Step(key, number, sname, agent, inputs, output, query, instruction))
    numbers = [s.number for s in chain]
    if numbers != sorted(numbers):
        p.add("chain 的 number 应按顺序递增")

    gates = _gates(_P(path.name, out), data.get("gates"), path.parent)
    for g in gates:
        if g.deliverable and g.deliverable not in dcodes:
            out.append(f"{path.name} gates {g.id}：deliverable {g.deliverable} 不是本产品的交付件编号")
    strict = data.get("strict") or {}
    if not isinstance(strict, dict):
        p.add("strict 应为对象")
        strict = {}
    card_items = _P(f"{path.name} strict", out).texts(strict, "card_items")
    return Product(code, name, domain, status, modules, tuple(deliverables), domains, frameworks, basis,
                   tuple(chain), gates, card_items, content_version(data), data)


def parse_module(path: Path, out: list[str]) -> Module | None:
    data = _read_yaml(path, out)
    if data is None:
        return None
    p = _P(path.name, out)
    _guard(p, data)
    code = p.text(data, "code")
    if code and not MODULE_CODE.match(code):
        p.add(f"code {code!r} 应为 2～8 位大写字母、数字和连字符")
    if code and path.stem != code.lower():
        p.add(f"文件名应为 {code.lower()}.yaml")
    name = p.text(data, "name", max_len=64)
    status = p.text(data, "status")
    if status and status not in STATUSES:
        p.add(f"status {status!r} 应为 ready 或 draft")
    products = p.texts(data, "products", required=True)
    adds = data.get("adds") or {}
    if not isinstance(adds, dict):
        p.add("adds 应为对象")
        adds = {}
    ap = _P(f"{path.name} adds", out)
    sections: dict[str, dict[str, tuple[str, ...]]] = {}
    raw_s = adds.get("sections") or {}
    if not isinstance(raw_s, dict):
        ap.add("sections 应为 {产品: {交付件: [章节]}}")
        raw_s = {}
    for prod, per in raw_s.items():
        if not isinstance(per, dict):
            ap.add(f"sections.{prod} 应为 {{交付件: [章节]}}")
            continue
        sections[str(prod)] = {str(d): _P(f"{path.name} adds.sections.{prod}", out).texts(per, d, required=True)
                               for d in per}
    raw_i = adds.get("instructions") or {}
    if not isinstance(raw_i, dict) or not all(isinstance(v, str) for v in raw_i.values()):
        ap.add("instructions 应为 {步骤键: 说明文字}")
        raw_i = {}
    instructions = {str(k): v.strip() for k, v in raw_i.items()}
    for k, v in instructions.items():
        _check_template(ap, f"instructions.{k}", v)
    gates = _gates(ap, adds.get("gates"), path.parent.parent / "products")
    return Module(code, name, status, products, sections, ap.texts(adds, "frameworks"), ap.texts(adds, "basis"),
                  instructions, gates, ap.texts(adds, "card_items"), ap.texts(adds, "kb_tags"),
                  content_version(data), data)


def _known_agents() -> set[str] | None:
    from ..agents import registry

    roles = registry.agents_dir() / "roles"
    if not roles.is_dir():
        return None
    return {f.stem for f in roles.glob("*.md")}


def load(base: Path | None = None, agents: set[str] | None = None) -> Catalog:
    """读 base/products 和 base/modules 下全部配置并做交叉校验；问题记在 Catalog.problems。"""
    base = base or catalog_dir()
    cat = Catalog()
    if agents is None:
        agents = _known_agents()
    for path in sorted((base / "products").glob("*.yaml")):
        prod = parse_product(path, cat.problems, agents)
        if prod is not None and prod.code:
            if prod.code in cat.products:
                cat.problems.append(f"{path.name}：产品代号 {prod.code} 重复")
            cat.products[prod.code] = prod
    for path in sorted((base / "modules").glob("*.yaml")):
        mod = parse_module(path, cat.problems)
        if mod is not None and mod.code:
            if mod.code in cat.modules:
                cat.problems.append(f"{path.name}：模块代号 {mod.code} 重复")
            cat.modules[mod.code] = mod
    _cross_check(cat)
    return cat


def _cross_check(cat: Catalog) -> None:
    out = cat.problems
    for prod in cat.products.values():
        for mc in prod.modules:
            mod = cat.modules.get(mc)
            if mod is None:
                out.append(f"产品 {prod.code}：模块 {mc} 没有配置文件")
            elif prod.code not in mod.products:
                out.append(f"产品 {prod.code} 列了模块 {mc}，但模块的 products 没有 {prod.code}")
    for mod in cat.modules.values():
        where = f"模块 {mod.code}"
        for pc in mod.products:
            prod = cat.products.get(pc)
            if prod is None:
                if not PRODUCT_CODE.match(pc):
                    out.append(f"{where}：products 里的 {pc!r} 不是产品代号")
                continue
            if mod.code not in prod.modules:
                out.append(f"{where} 声明可挂 {pc}，但产品 {pc} 的 modules 没有 {mod.code}")
        for pc, per in mod.sections.items():
            if pc not in mod.products:
                out.append(f"{where}：sections 里的 {pc} 不在模块的 products 里")
                continue
            prod = cat.products.get(pc)
            for dcode in per:
                if prod is not None and prod.deliverable(dcode) is None:
                    out.append(f"{where}：产品 {pc} 没有交付件 {dcode}")
        step_keys = {s.key for pc in mod.products if pc in cat.products for s in cat.products[pc].chain}
        for k in mod.instructions:
            if step_keys and k not in step_keys:
                out.append(f"{where}：instructions 的步骤 {k} 在所挂产品的交付链里都没有")
        dcodes = {d.code for pc in mod.products if pc in cat.products for d in cat.products[pc].deliverables}
        for g in mod.gates:
            if g.deliverable and dcodes and g.deliverable not in dcodes:
                out.append(f"{where} gates {g.id}：所挂产品都没有交付件 {g.deliverable}")


def validate(base: Path | None = None) -> list[str]:
    return load(base).problems


# ---- 运行时读取（带缓存，文件变了自动重新读） ----

DEFAULT_PRODUCT = "SOC"   # 一期项目的产品；A3 起立项必须选产品、运行时读项目快照


class CatalogError(RuntimeError):
    """配置缺失或有问题。消息只写类别，给运维看；详情用 catalog-check。"""


_lock = threading.Lock()
_cache: tuple[tuple, Catalog] | None = None


def _stamp(base: Path) -> tuple:
    files = [*sorted((base / "products").glob("*.yaml")), *sorted((base / "modules").glob("*.yaml")),
             *sorted((base / "products" / RULES_DIR).glob("*"))]
    return (str(base), *((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files))


def catalog() -> Catalog:
    global _cache
    base = catalog_dir()
    stamp = _stamp(base)
    with _lock:
        if _cache is not None and _cache[0] == stamp:
            return _cache[1]
    cat = load(base)
    with _lock:
        _cache = (stamp, cat)
    return cat


def product(code: str) -> Product:
    """取一个可用（ready）的产品配置；整套配置有问题时一律拒绝，不带病运行。"""
    cat = catalog()
    if cat.problems:
        raise CatalogError(f"服务产品配置有 {len(cat.problems)} 个问题，请运行 python -m app.cli catalog-check")
    prod = cat.products.get(code)
    if prod is None or prod.status != "ready":
        raise CatalogError(f"服务产品 {code} 没有可用的配置")
    return prod


def render(template: str, prod: Product, step_number: int = 0, module_names: tuple[str, ...] = (),
           module_instructions: str = "") -> str:
    """填提示词模板（变量见 TEMPLATE_VARS）；没有变量的模板原样返回。"""
    if not _template_vars(template):
        return template
    return template.format(product_name=prod.name, domains="、".join(prod.domains),
                           frameworks="、".join(prod.frameworks), basis="、".join(prod.basis), number=step_number,
                           modules="、".join(module_names), module_instructions=module_instructions)
