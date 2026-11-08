"""请求参数校验失败（422）的返回内容。

FastAPI 默认把出错字段的原始输入（input）和校验上下文（ctx）原样放进 422 返回里：
字段名写错时会回显口令，提问过长时会回显整段提问。这些内容一旦被前置网关、代理或浏览器插件记录，
就可能把客户原文、口令带进日志（红线 4）。这里只保留“哪个字段、什么问题”，不回显任何输入值。
"""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# 只保留这几项；input、ctx、url 一律去掉
_KEEP = ("loc", "msg", "type")


def safe_errors(exc: RequestValidationError) -> list[dict]:
    return [{k: (list(e[k]) if k == "loc" else e[k]) for k in _KEEP if k in e} for e in exc.errors()]


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": safe_errors(exc)})
