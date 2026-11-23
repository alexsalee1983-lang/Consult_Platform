"""对象存储：项目区与知识区分桶，只经 S3 协议访问（一期为自建 MinIO，可换成其他 S3 兼容存储）。

规则：
- 只有两个区：project（项目区：纪要、客户资料、交付件）和 knowledge（知识区：去标识后的知识原件）。
  两区分桶，调用方必须明确写区名；能不能访问某个区由工具层硬规则判断（app/agents/tools.py），这里不做权限。
- 桶默认开启服务端加密（SSE-S3，主密钥由 MinIO 的 KMS 配置提供），建桶时设置，健康检查时核对。
- 对象键由调用方按“项目代号/类别/编号”等规则生成；这里只拒绝 ..、开头的 /、控制字符等不安全写法。
- 日志和异常信息不带对象内容。
- 最小权限（三期 G1）：后端用应用账号连接，策略只允许两个区的桶的对象读写、列举和读加密配置，不能建删桶、
  改加密和桶策略，没有管理权限；运维用根账号执行 `storage-provision` 一次性建桶、加密、建账号、挂策略。
  生产环境后端用根账号连接时拒绝启动（core/bootstrap.py）。
"""
from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.config import get_settings

AREAS = ("project", "knowledge")
APP_POLICY_NAME = "consult-app-rw"
# 应用账号能做的事：桶级只读（列举、位置、加密配置、分段上传列表），对象级读写删与分段上传
_BUCKET_ACTIONS = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads",
                   "s3:GetEncryptionConfiguration"]
_OBJECT_ACTIONS = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload",
                   "s3:ListMultipartUploadParts"]
_KEY_RE = re.compile(r"^[A-Za-z0-9一-鿿][A-Za-z0-9一-鿿._/-]{0,511}$")

_lock = threading.Lock()
_client = None
_ready = False


class StorageError(RuntimeError):
    pass


class StorageDisabled(StorageError):
    pass


@dataclass(frozen=True)
class StoredObject:
    area: str
    key: str
    size: int
    etag: str
    content_type: str


def enabled() -> bool:
    return bool(get_settings().minio_endpoint)


def bucket(area: str) -> str:
    s = get_settings()
    if area == "project":
        return s.minio_bucket_project
    if area == "knowledge":
        return s.minio_bucket_knowledge
    raise ValueError(f"未知的存储区：{area!r}（只能是 project 或 knowledge）")


def check_key(key: str) -> str:
    if not _KEY_RE.match(key) or ".." in key.split("/") or "//" in key:
        raise ValueError("对象键不合法：只能用字母、数字、汉字和 . _ - /，不能以 / 开头，不能含 ..")
    return key


def _get_client():
    global _client
    if not enabled():
        raise StorageDisabled("对象存储未启用（MINIO_ENDPOINT 未配置）")
    with _lock:
        if _client is None:
            from minio import Minio

            s = get_settings()
            _client = Minio(s.minio_endpoint, access_key=s.minio_access_key, secret_key=s.minio_secret_key,
                            secure=s.minio_secure)
        return _client


def reset() -> None:
    """配置变化后（测试里）重新建客户端。"""
    global _client, _ready
    with _lock:
        _client, _ready = None, False


def ensure_buckets() -> None:
    """两个区的桶不存在就建，并设置默认服务端加密。可重复调用。"""
    global _ready
    if _ready:
        return
    from minio.sseconfig import Rule, SSEConfig

    c = _get_client()
    for area in AREAS:
        name = bucket(area)
        if not c.bucket_exists(name):
            c.make_bucket(name)
        if c.get_bucket_encryption(name) is None:   # 已加密就不再改（应用账号没有改加密配置的权限）
            c.set_bucket_encryption(name, SSEConfig(Rule.new_sse_s3_rule()))
    _ready = True


def app_policy(buckets: list[str]) -> dict[str, Any]:
    """应用账号的访问策略（S3 策略语法，MinIO 兼容）：只覆盖给定的桶。"""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": list(_BUCKET_ACTIONS),
         "Resource": [f"arn:aws:s3:::{b}" for b in buckets]},
        {"Effect": "Allow", "Action": list(_OBJECT_ACTIONS),
         "Resource": [f"arn:aws:s3:::{b}/*" for b in buckets]},
    ]}


@dataclass
class ProvisionReport:
    created_buckets: list[str] = field(default_factory=list)
    account: str = ""
    policy: str = APP_POLICY_NAME


def provision(root_user: str, app_access_key: str, app_secret_key: str, *, root_password: str = "",
              client: Any = None, admin: Any = None) -> ProvisionReport:
    """运维用根账号执行一次：两个区的桶不存在就建并设默认加密，建（或更新口令）应用账号，挂只限这两个桶的策略。
    可重复执行。根账号口令只在本次调用中使用，不写进平台配置。"""
    if not app_access_key or app_access_key == root_user:
        raise StorageError("应用账号不能与对象存储根账号相同（MINIO_ACCESS_KEY 要另起一个名字）")
    if len(app_secret_key) < 20:
        raise StorageError("应用账号口令至少 20 位（MINIO_SECRET_KEY）")
    s = get_settings()
    if client is None or admin is None:
        from minio import Minio
        from minio.credentials import StaticProvider
        from minio.minioadmin import MinioAdmin

        client = client or Minio(s.minio_endpoint, access_key=root_user, secret_key=root_password,
                                 secure=s.minio_secure)
        admin = admin or MinioAdmin(endpoint=s.minio_endpoint, secure=s.minio_secure,
                                    credentials=StaticProvider(root_user, root_password))
    from minio.sseconfig import Rule, SSEConfig

    names = [bucket(a) for a in AREAS]
    rep = ProvisionReport(account=app_access_key)
    for name in names:
        if not client.bucket_exists(name):
            client.make_bucket(name)
            rep.created_buckets.append(name)
        if client.get_bucket_encryption(name) is None:
            client.set_bucket_encryption(name, SSEConfig(Rule.new_sse_s3_rule()))
    admin.user_add(app_access_key, app_secret_key)
    admin.policy_add(APP_POLICY_NAME, policy=app_policy(names))
    try:
        admin.attach_policy([APP_POLICY_NAME], user=app_access_key)
    except Exception as e:  # noqa: BLE001
        if "AlreadyApplied" not in str(e):   # 已挂过策略：MinIO 返回 XMinioAdminPolicyChangeAlreadyApplied
            raise
    return rep


def health() -> str:
    if not enabled():
        return "not enabled"
    try:
        c = _get_client()
        for area in AREAS:
            name = bucket(area)
            if not c.bucket_exists(name):
                return f"down: 桶 {name} 不存在"
            cfg = c.get_bucket_encryption(name)
            if cfg is None or not cfg.rule:
                return f"down: 桶 {name} 未开启服务端加密"
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"down: {type(e).__name__}"


def put(area: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
    ensure_buckets()
    name, key = bucket(area), check_key(key)
    r = _get_client().put_object(name, key, io.BytesIO(data), len(data), content_type=content_type)
    return StoredObject(area=area, key=key, size=len(data), etag=r.etag, content_type=content_type)


def get(area: str, key: str) -> bytes:
    name, key = bucket(area), check_key(key)
    resp = _get_client().get_object(name, key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def stat(area: str, key: str) -> StoredObject | None:
    from minio.error import S3Error

    name, key = bucket(area), check_key(key)
    try:
        o = _get_client().stat_object(name, key)
    except S3Error as e:
        if e.code in ("NoSuchKey", "NoSuchObject"):
            return None
        raise
    return StoredObject(area=area, key=key, size=o.size, etag=o.etag, content_type=o.content_type or "")


def list_keys(area: str, prefix: str) -> list[str]:
    """列出某个前缀下的全部对象键（项目资料删除时清点用）。prefix 必须以 / 结尾，避免 BANK-A 误匹配 BANK-AB。"""
    if not prefix.endswith("/"):
        raise ValueError("前缀必须以 / 结尾")
    name, prefix = bucket(area), check_key(prefix)
    return [o.object_name for o in _get_client().list_objects(name, prefix=prefix, recursive=True)]


def delete(area: str, key: str) -> None:
    """删除对象（不存在时不报错）。项目区到期删除、知识下线时调用，调用方负责写审计。"""
    _get_client().remove_object(bucket(area), check_key(key))


def is_encrypted(area: str, key: str) -> bool:
    """对象是否已做服务端加密（看响应头 x-amz-server-side-encryption）。"""
    o = _get_client().stat_object(bucket(area), check_key(key))
    headers = {k.lower(): v for k, v in (o.metadata or {}).items()}
    return bool(headers.get("x-amz-server-side-encryption"))
