"""平台压测脚本（M5 ⑥）。只在测试机本地运行，只用虚拟项目和压测账号，不用于生产环境。

用法见 loadtest/README.md。每个虚拟用户用一个独立的压测账号（动态码防重放，不能共用），
按顾问的日常操作比例访问（决策记录 2026-11-13）：项目列表与详情、证据、交付件、知识检索、确认卡、
顾问对话（经网关走模型）、录入观察、上传资料、写章节（D5，每人一章）、导出 Word（D5）、发起交付链（同一项目同时
只能有一个运行，409 视为正常）。
结束时按阈值判定（docs/压测方案.md）：失败数为 0；普通接口 p95 < 1 秒；知识检索 p95 < 2 秒；
顾问对话 p95 < CHAT_P95_MS（模拟模型默认 5 秒，真实模型轮次按需放宽）。不满足时退出码 1。
"""
import base64
import hashlib
import hmac
import itertools
import json
import os
import secrets
import string
import struct
import threading
import time

from locust import HttpUser, between, events, task


def _tag() -> str:
    """上传文本的唯一标记：只用字母。带长串数字会被机密级检测当成银行卡号拦截（#78 首轮测试）。"""
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))

ACCOUNTS = os.environ.get("LOADTEST_ACCOUNTS", os.path.join(os.path.dirname(__file__), "accounts.json"))
API_P95_MS = int(os.environ.get("API_P95_MS", "1000"))
SEARCH_P95_MS = int(os.environ.get("SEARCH_P95_MS", "2000"))
CHAT_P95_MS = int(os.environ.get("CHAT_P95_MS", "5000"))
AGENT = os.environ.get("LOADTEST_AGENT", "consultant-lead")

# 检索词和提问都是通用的安全管理用语，不含任何客户信息
QUERIES = ["值班", "特权账号", "日志留存", "备份恢复", "应急预案", "访问控制", "漏洞管理", "安全培训"]
QUESTIONS = ["夜间值守安排一般怎么做差距分析？", "特权账号复核周期的常见整改建议有哪些？",
             "日志留存不足六个月时，整改建议怎么写？", "备份恢复演练的记录要包含哪些要素？"]

with open(ACCOUNTS, encoding="utf-8") as f:
    _DATA = json.load(f)
PROJECT = _DATA["project"]
_accounts = itertools.cycle(_DATA["users"])
_lock = threading.Lock()


def totp(secret: str, at: float | None = None) -> str:
    s = secret.strip().upper()
    s += "=" * (-len(s) % 8)
    h = hmac.new(base64.b32decode(s), struct.pack(">Q", int(at or time.time()) // 30), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000)


class Consultant(HttpUser):
    wait_time = between(3, 10)   # 模拟人操作的间隔

    def on_start(self) -> None:
        with _lock:
            acc = next(_accounts)
        self.username = acc["username"]
        self.csrf = ""
        self.conv = 0
        self.n = 0
        self.seq = int(acc["username"].rsplit("-", 1)[1])   # lt-03 写 D5 第 3 章
        r = self.client.post("/api/auth/login", name="auth.login",
                             json={"username": acc["username"], "password": acc["password"],
                                   "totp": totp(acc["totp_secret"])})
        if r.status_code == 200:
            self.csrf = r.json().get("csrf_token", "")
        r = self.client.post(f"/api/projects/{PROJECT}/conversations", name="conversation.create",
                             json={"agent_code": AGENT}, headers=self._h())
        if r.status_code == 201:
            self.conv = r.json()["id"]

    def _h(self) -> dict:
        return {"X-CSRF-Token": self.csrf}

    @task(4)
    def projects(self) -> None:
        self.client.get("/api/projects", name="projects.list")

    @task(4)
    def project(self) -> None:
        self.client.get(f"/api/projects/{PROJECT}", name="projects.detail")

    @task(2)
    def me(self) -> None:
        self.client.get("/api/auth/me", name="auth.me")

    @task(3)
    def evidences(self) -> None:
        self.client.get(f"/api/projects/{PROJECT}/evidences", name="evidences.list")

    @task(3)
    def deliverable(self) -> None:
        self.client.get(f"/api/projects/{PROJECT}/deliverables/D4", name="deliverables.detail")

    @task(2)
    def confirmations(self) -> None:
        self.client.get("/api/confirmations", name="confirmations.list")

    @task(3)
    def search(self) -> None:
        self.n += 1
        self.client.get("/api/kb/search", params={"q": QUERIES[self.n % len(QUERIES)]}, name="kb.search")

    @task(1)
    def note(self) -> None:
        self.n += 1
        self.client.post(f"/api/projects/{PROJECT}/notes", name="notes.create", headers=self._h(), json={
            "kind": "observation", "title": f"压测观察 {self.username}-{self.n}",
            "content": "压测用的虚拟现场观察：机房巡检记录按周归档，未发现异常。"})

    @task(1)
    def upload(self) -> None:
        self.n += 1
        text = f"压测用的虚拟资料 {self.username}-{self.n}-{_tag()}：值班表按周排班，交接班有签字记录。"
        self.client.post(f"/api/projects/{PROJECT}/documents", name="documents.upload", headers=self._h(),
                         files={"file": ("loadtest.txt", text.encode("utf-8"), "text/plain")},
                         data={"title": "压测资料"})

    @task(2)
    def section(self) -> None:
        self.n += 1
        with self.client.put(f"/api/projects/{PROJECT}/deliverables/D5/sections/{self.seq}", name="sections.write",
                             headers=self._h(), catch_response=True,
                             json={"body": f"压测正文第 {self.n} 稿：夜间值守安排需补充交接记录。[推断]"}) as r:
            # 409 是业务上的版本冲突（同章同时修改），接口正确拒绝，不算失败
            if r.status_code in (200, 201, 409):
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")

    @task(1)
    def export(self) -> None:
        with self.client.get(f"/api/projects/{PROJECT}/deliverables/D5/export", name="deliverables.export",
                             catch_response=True) as r:
            if r.status_code == 200 and r.content[:2] == b"PK":
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")

    @task(1)
    def start_run(self) -> None:
        with self.client.post(f"/api/projects/{PROJECT}/runs", name="runs.start", headers=self._h(),
                              catch_response=True) as r:
            if r.status_code in (202, 409):   # 409：已有未结束的运行，属于正常业务结果
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")

    @task(1)
    def chat(self) -> None:
        if not self.conv:
            return
        self.n += 1
        with self.client.post(f"/api/conversations/{self.conv}/messages", name="chat.stream", headers=self._h(),
                              json={"message": QUESTIONS[self.n % len(QUESTIONS)]}, stream=True,
                              catch_response=True) as r:
            body = r.content.decode("utf-8", "replace")   # 读完整个流，计时到最后一段
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
            elif "event: done" not in body:
                r.failure("流式回复没有正常结束")
            else:
                r.success()


@events.quitting.add_listener
def _judge(environment, **_kw) -> None:
    stats = environment.stats
    problems = []
    if stats.total.num_failures:
        problems.append(f"失败 {stats.total.num_failures} 次")
    for entry in stats.entries.values():
        if not entry.num_requests:
            continue
        p95 = entry.get_response_time_percentile(0.95)
        limit = SEARCH_P95_MS if entry.name == "kb.search" else CHAT_P95_MS if entry.name == "chat.stream" \
            else API_P95_MS
        if p95 > limit:
            problems.append(f"{entry.name} p95 {p95:.0f}ms > {limit}ms")
    if problems:
        print("压测未达标：" + "；".join(problems))
        environment.process_exit_code = 1
    else:
        print("压测达标：失败 0，各接口 p95 均在阈值内")
        environment.process_exit_code = 0
