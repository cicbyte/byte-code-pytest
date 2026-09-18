"""byte-code-pytest：pytest ↔ ByteCode 平台桥。

pytest --bcode 启用；session 结束把整批结果上报平台 POST /api/v1/projects/{id}/test-runs
（认证走 agent bc_ key，能力 test_execute）。三层用例映射：

1. 显式映射：@pytest.mark.bytecode(case=123) → test_case_id=123
2. 自动同步：--bcode-sync 时按 external_key（pytest nodeid）查平台用例，
   不存在则自动创建（title=nodeid, module=pytest）；默认关闭
3. 仅记录：无映射时 test_case_id=0，external_key 照记（历史可追溯）

纯标准库实现：CI 环境安装即用，零第三方依赖。
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

MESSAGE_LIMIT = 4000  # 客户端截断（服务端兜底 8000）
TIMEOUT = 30

# 模块级状态：runtest_logreport 钩子不传 config，经此桥接。
# xdist 下每个 worker 是独立模块实例，各自上报/各不相扰
_STATE = None


# ---------------- 命令行 / 配置 ----------------

def pytest_addoption(parser):
    group = parser.getgroup("bcode")
    group.addoption("--bcode", action="store_true", default=False,
                    help="启用 ByteCode 平台执行结果上报")
    group.addoption("--bcode-url", action="store", default=None,
                    help="平台 API 地址（缺省 BCODE_URL），如 https://bc.example.com/api")
    group.addoption("--bcode-key", action="store", default=None,
                    help="agent bc_ key（缺省 BCODE_KEY；凭据只放环境变量/CI secret，勿入库）")
    group.addoption("--bcode-project", action="store", type=int, default=None,
                    help="平台项目 id（缺省 BCODE_PROJECT）")
    group.addoption("--bcode-source", action="store", default="pytest",
                    choices=["pytest", "ci", "junit", "manual"],
                    help="执行来源标识（默认 pytest）")
    group.addoption("--bcode-env", action="store", default=None,
                    help="环境标识 local/ci（缺省 BCODE_ENV 或 local）")
    group.addoption("--bcode-branch", action="store", default=None,
                    help="覆盖 git 分支自动探测")
    group.addoption("--bcode-sync", action="store_true", default=False,
                    help="未映射用例按 nodeid 自动创建平台用例（默认关闭，仅 external_key 记录）")
    group.addoption("--bcode-strict", action="store_true", default=False,
                    help="上报失败时让 pytest 非零退出（默认仅告警）")
    group.addoption("--bcode-dump", action="store", default=None, metavar="PATH",
                    help="离线模式：结果落盘 JSON 而非直传（后续 bcode test --upload 补传）；"
                         "此时无需 url/key/project")


class _State:
    """单次 session 的采集状态"""

    def __init__(self):
        self.enabled = False
        self.dump_path = None
        self.start_ts = None
        self.case_ids = {}   # nodeid -> 显式映射的平台用例 id
        self.results = {}    # nodeid -> {status, duration_ms, message}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "bytecode(case): 显式映射平台测试用例 id，如 @pytest.mark.bytecode(case=123)",
    )
    global _STATE
    state = _State()
    config._bcode = state
    _STATE = state
    if not config.getoption("--bcode"):
        return
    dump_path = config.getoption("--bcode-dump")
    url = config.getoption("--bcode-url") or os.environ.get("BCODE_URL")
    key = config.getoption("--bcode-key") or os.environ.get("BCODE_KEY")
    project = config.getoption("--bcode-project")
    if project is None:
        p = os.environ.get("BCODE_PROJECT")
        project = int(p) if p and p.isdigit() else None
    if dump_path:
        # 离线模式：不上传，三件套不要求（项目归属由补传时的 .bc/project 决定）
        pass
    elif not url or not key or not project:
        raise pytest.UsageError(
            "bcode: 缺少上报配置——需要 --bcode-url/--bcode-key/--bcode-project "
            "（或环境变量 BCODE_URL/BCODE_KEY/BCODE_PROJECT）；"
            "离线场景可改用 --bcode-dump <path> 落盘后 bcode test --upload 补传"
        )
    state.enabled = True
    state.dump_path = dump_path
    state.base_url = url.rstrip("/") if url else ""
    state.key = key or ""
    state.project = int(project) if project else 0
    state.source = config.getoption("--bcode-source")
    state.env = config.getoption("--bcode-env") or os.environ.get("BCODE_ENV") or "local"
    state.branch_override = config.getoption("--bcode-branch")
    state.sync = config.getoption("--bcode-sync")
    state.strict = config.getoption("--bcode-strict")


def pytest_sessionstart(session):
    state = getattr(session.config, "_bcode", None)
    if state is not None and state.enabled:
        state.start_ts = time.time()


def pytest_collection_modifyitems(session, config, items):
    # 显式映射在收集期一次采齐（logreport 里拿不到 item，makereport 又会
    # 干扰其它插件的包装链，故不在执行期挂钩）
    state = getattr(config, "_bcode", None)
    if state is None or not state.enabled:
        return
    for item in items:
        marker = item.get_closest_marker("bytecode")
        if marker is None:
            continue
        case = marker.kwargs.get("case")
        if isinstance(case, int) and case > 0:
            state.case_ids[item.nodeid] = case


def pytest_runtest_logreport(report):
    if _STATE is None or not _STATE.enabled:
        return
    _collect(_STATE, report)


def _truncate(text):
    if len(text) <= MESSAGE_LIMIT:
        return text
    return text[:MESSAGE_LIMIT] + "\n...[truncated by byte-code-pytest]"


def _collect(state, report):
    nodeid = report.nodeid
    entry = state.results.setdefault(
        nodeid, {"status": "pass", "duration_ms": 0, "message": ""}
    )
    # round 而非 int 截断：亚毫秒用例截断成 0 会在 UI 显示为 "-"（冒烟反馈）
    entry["duration_ms"] += round(report.duration * 1000)

    if report.when == "call":
        if report.passed:
            entry["status"] = "pass"
        elif report.failed:
            entry["status"] = "fail"
        else:
            entry["status"] = "skip"
    elif report.when == "setup":
        # setup 失败 = fixture/收集错误，pytest 语义里整条用例 error；
        # setup 即 skip（@pytest.mark.skip / skipif）也在此定型
        if report.failed:
            entry["status"] = "error"
        elif report.skipped:
            entry["status"] = "skip"
    # teardown 失败不改变用例结论，附加提示信息

    if report.failed:
        text = str(report.longrepr) if report.longrepr else "failed"
        extra = ""
        if report.when == "teardown":
            extra = "\n[teardown failed]\n"
        entry["message"] = _truncate((extra + text) if not entry["message"] else entry["message"] + extra + text)


def pytest_sessionfinish(session, exitstatus):
    state = getattr(session.config, "_bcode", None)
    if state is None or not state.enabled or not state.results:
        return
    try:
        if state.dump_path:
            count = _dump(state, state.dump_path)
            print(
                "\n[bcode] 已离线落盘 {}（{} 用例）——补传：bcode test --upload {}".format(
                    state.dump_path, count, state.dump_path
                )
            )
            return
        run_id = _upload(state)
        print(
            "\n[bcode] 已上报执行记录 run #{}（{} 用例，项目 {}）".format(
                run_id, len(state.results), state.project
            )
        )
    except Exception as exc:  # noqa: BLE001 —— 上报失败不应吞掉原因
        msg = "[bcode] 上报失败: {}".format(exc)
        if state.strict:
            raise RuntimeError(msg) from exc
        print("\n" + msg)
        print("[bcode] 测试本身的结果不受影响（--bcode-strict 可改为强制失败）")


# ---------------- 平台 API ----------------

def _request(state, method, path, body=None):
    url = state.base_url + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer {}".format(state.key))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("HTTP {} {}: {}".format(exc.code, path, detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("连接失败 {}: {}".format(url, exc.reason)) from exc
    # 响应壳 {code, message, data}（部分端点为 result），业务数据在内层。
    # 成功 code 实测有两种：0（GoFrame 原生）与 200（响应中间件归一），都放行
    code = payload.get("code")
    if code not in (None, 0, 200):
        raise RuntimeError("平台拒绝 {}: {}".format(path, payload.get("message")))
    return payload.get("data", payload.get("result", payload))


def _git(*args):
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 —— 非 git 环境静默降级
        return ""


def _resolve_case_id(state, nodeid):
    """映射三层：显式 marker > --bcode-sync 查/建 > 0（仅 external_key）"""
    if nodeid in state.case_ids:
        return state.case_ids[nodeid]
    if not state.sync or state.dump_path:
        # 离线 dump 无平台可查/建，映射留 0 由补传后的下一次运行补齐
        return 0
    found = _request(
        state, "GET",
        "/v1/projects/{}/test-cases?externalKey={}&pageSize=1".format(
            state.project, urllib.request.quote(nodeid, safe="")
        ),
    )
    if found and found.get("list"):
        return found["list"][0]["id"]
    created = _request(
        state, "POST", "/v1/projects/{}/test-cases".format(state.project),
        {
            "title": nodeid,
            "module": "pytest",
            "priority": "P2",
            "externalKey": nodeid,
        },
    )
    return created.get("id", 0)


def _build_payload(state):
    cases = []
    for nodeid, entry in state.results.items():
        cases.append(
            {
                "testCaseId": _resolve_case_id(state, nodeid),
                "externalKey": nodeid,
                "title": nodeid.rsplit("::", 1)[-1],
                "status": entry["status"],
                "durationMs": entry["duration_ms"],
                "message": entry["message"],
            }
        )
    finished = time.time()
    started = state.start_ts or finished
    return {
        "source": state.source,
        "branch": state.branch_override or _git("rev-parse", "--abbrev-ref", "HEAD"),
        "gitSha": _git("rev-parse", "HEAD"),
        "env": state.env,
        "startedAt": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
        "finishedAt": datetime.fromtimestamp(finished).strftime("%Y-%m-%d %H:%M:%S"),
        "durationMs": int((finished - started) * 1000),
        "cases": cases,
    }


def _dump(state, path):
    """离线落盘：format 壳供 bcode test --upload 内容嗅探"""
    doc = {"format": "bcode-test-run", "version": 1, "payload": _build_payload(state)}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    return len(doc["payload"]["cases"])


def _upload(state):
    payload = _build_payload(state)
    created = _request(
        state, "POST", "/v1/projects/{}/test-runs".format(state.project), payload
    )
    return created.get("id", 0)
