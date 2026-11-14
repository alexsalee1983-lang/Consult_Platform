"""请求参数校验失败（422）的返回内容。

FastAPI 默认把出错字段的原始输入（input）和校验上下文（ctx）原样放进 422 返回里：
字段名写错时会回显口令，提问过长时会回显整段提问。这些内容一旦被前置网关、代理或浏览器插件记录，
就可能把客户原文、口令带进日志（红线 4）。这里只保留“哪个字段、什么问题”，不回显任何输入值。
"""
import json
import re

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# 只保留这几项；input、ctx、url 一律去掉
_KEEP = ("loc", "msg", "type")


def safe_errors(exc: RequestValidationError) -> list[dict]:
    return [{k: (list(e[k]) if k == "loc" else e[k]) for k in _KEEP if k in e} for e in exc.errors()]


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": safe_errors(exc)})


async def out_of_range_handler(request: Request, exc: Exception) -> JSONResponse:
    """数值或文本超出数据库列的范围（编号填了超大整数、文本超过列长度）：按参数错误返回 422，不报 500。

    SQLite 抛 OverflowError，PostgreSQL 抛 DataError（数值越界、字符串超长）。不回显输入，也不记原文。
    """
    return JSONResponse(status_code=422, content={"detail": [{"loc": [], "msg": "参数超出允许范围",
                                                               "type": "out_of_range"}]})


# 控制字符（保留制表、换行、回车）、孤立代理项、U+FFFE/U+FFFF：写不进 Word（XML 1.0 不允许），
# PostgreSQL 文本列也不收 NUL。渗透测试（M5 ⑦）发现交付件正文带这类字符后导出一直 500，在入口统一拒收。
BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


def _has_bad_chars(value: object, depth: int = 0) -> bool:
    if depth > 64:
        return True
    if isinstance(value, str):
        return BAD_CHARS.search(value) is not None
    if isinstance(value, dict):
        return any(_has_bad_chars(k, depth + 1) or _has_bad_chars(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_bad_chars(v, depth + 1) for v in value)
    return False


async def reject_control_chars(request: Request) -> None:
    """全局依赖：JSON 请求体和查询参数里不许有控制字符。不回显输入。"""
    bad = any(BAD_CHARS.search(v) for _, v in request.query_params.multi_items())
    if not bad and request.method in {"POST", "PUT", "PATCH", "DELETE"} \
            and request.headers.get("content-type", "").startswith("application/json"):
        raw = await request.body()
        if raw:
            try:
                bad = _has_bad_chars(json.loads(raw))
            except ValueError:
                return   # 不是合法 JSON：交给 FastAPI 按 422 处理
    if bad:
        raise HTTPException(422, "内容含有不允许的控制字符")
