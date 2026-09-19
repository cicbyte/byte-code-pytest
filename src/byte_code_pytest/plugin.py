"""byte-code-pytest：pytest ↔ ByteCode 平台桥。

pytest --bcode 启用；session 结束把整批结果上报平台 POST /api/v1/projects/{id}/test-runs
（认证走 agent bc_ key，能力 test_execute）。三层用例映射：

1. 显式映射：@pytest.mark.bytecode(case=123) → test_case_id=123
2. 自动同步：--bcode-sync 时按 external_key（pytest nodeid）查平台用例，
   不存在则自动创建（标题优先 marker title / docstring 首行）；已存在且
   marker 带元数据时 PUT 回写（幂等 upsert，测试代码为唯一事实源）；默认关闭
3. 仅记录：无映射时 test_case_id=0，external_key 照记（历史可追溯）

增强：幂等键（同键重发命中既有 run 不产生重复）、连接级失败重试、
失败/错误用例附件回传、GitHub Actions ::error 注解与 Step Summary、
skip_report / --bcode-exclude 排除不上报。

纯标准库实现：CI 环境安装即用，零第三方依赖。
"""

import fnmatch
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

MESSAGE_LIMIT = 4000  # 客户端截断（服务端兜底 8000）
TIMEOUT = 30
RETRIES = 2  # 仅「连接未建立」类失败重试（见 _request），指数退避

# 模块级状态：runtest_logreport 钩子不传 config，经此桥接。
# xdist 下每个 worker 是独立模块实例，各自上报/各不相扰
_STATE = None

# marker 元数据键 → 平台用例字段（pre/expected 对应 preconditions/expectedResult）
_META_FIELDS = (
    ("title", "title"),
    ("module", "module"),
    ("category", "category"),
    ("priority", "priority"),
    ("pre", "preconditions"),
    ("expected", "expectedResult"),
)


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
                    help="未映射用例按 nodeid 自动创建平台用例（默认关闭，仅 external_key 记录）；"
                         "marker 元数据/docstring 首行随创建写入，已存在则回写更新")
    group.addoption("--bcode-exclude", action="append", default=None, metavar="PATTERN",
                    help="fnmatch 排除不上报的用例（按 nodeid，可重复）；"
                         "单用例粒度也可用 @pytest.mark.bytecode(skip_report=True)")
    group.addoption("--bcode-screenshots", action="store", default="screenshots", metavar="DIR",
                    help="失败/错误用例截图目录（默认 screenshots）：文件名以净化后的 nodeid "
                         "为前缀即自动挂到对应用例行；另可用 marker attach_on_fail 显式指定")
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
        self.case_ids = {}     # nodeid -> 显式映射的平台用例 id
        self.meta = {}         # nodeid -> {title/module/...}（marker > docstring 首行）
        self.attach = {}       # nodeid -> [显式附件路径]（marker attach_on_fail）
        self.excluded = set()  # skip_report / --bcode-exclude 命中的 nodeid
        self.results = {}      # nodeid -> {status, duration_ms, message}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "bytecode(case, title, module, category, priority, pre, expected, "
        "skip_report, attach_on_fail): 关联/描述平台测试用例；case=平台用例 id，"
        "skip_report=True 不上报，attach_on_fail=[路径] 失败时回传附件",
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
    state.exclude_patterns = config.getoption("--bcode-exclude") or []
    state.screenshots_dir = config.getoption("--bcode-screenshots")


def pytest_sessionstart(session):
    state = getattr(session.config, "_bcode", None)
    if state is not None and state.enabled:
        state.start_ts = time.time()


def pytest_collection_modifyitems(session, config, items):
    # 显式映射与元数据在收集期一次采齐（logreport 里拿不到 item，makereport 又会
    # 干扰其它插件的包装链，故不在执行期挂钩）
    state = getattr(config, "_bcode", None)
    if state is None or not state.enabled:
        return
    for item in items:
        nodeid = item.nodeid
        if any(fnmatch.fnmatch(nodeid, pat) for pat in state.exclude_patterns):
            state.excluded.add(nodeid)
            continue
        marker = item.get_closest_marker("bytecode")
        meta = {}
        if marker is not None:
            case = marker.kwargs.get("case")
            if isinstance(case, int) and case > 0:
                state.case_ids[nodeid] = case
            if marker.kwargs.get("skip_report"):
                state.excluded.add(nodeid)
                continue
            for src, _dst in _META_FIELDS:
                value = marker.kwargs.get(src)
                if isinstance(value, str) and value:
                    meta[src] = value
            att = marker.kwargs.get("attach_on_fail")
            if att:
                state.attach[nodeid] = [str(p) for p in att]
        # 标题兜底链：marker title > 测试函数 docstring 首行（sync 建用例时生效）
        if not meta.get("title"):
            doc = getattr(item.obj, "__doc__", None)
            if doc and doc.strip():
                meta["title"] = doc.strip().splitlines()[0].strip()
        if meta:
            state.meta[nodeid] = meta


def pytest_runtest_logreport(report):
    if _STATE is None or not _STATE.enabled:
        return
    if report.nodeid in _STATE.excluded:
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
            _warn_unmapped(state)
            return
        run_id, duplicate = _upload(state)
        print(
            "\n[bcode] 已上报执行记录 run #{}（{} 用例，项目 {}{}）".format(
                run_id, len(state.results), state.project,
                "，幂等重发命中" if duplicate else ""
            )
        )
        try:
            count = _upload_attachments(state, run_id)
            if count:
                print("[bcode] 已回传 {} 个失败/错误用例附件".format(count))
        except Exception as exc:  # noqa: BLE001 —— 附件是增强能力，失败不推翻上报结论
            print("[bcode] 附件回传失败: {}".format(exc))
        _warn_unmapped(state)
        _ci_output(state, run_id, duplicate)
    except Exception as exc:  # noqa: BLE001 —— 上报失败不应吞掉原因
        msg = "[bcode] 上报失败: {}".format(exc)
        if state.strict:
            raise RuntimeError(msg) from exc
        print("\n" + msg)
        print("[bcode] 测试本身的结果不受影响（--bcode-strict 可改为强制失败）")


# ---------------- 平台 API ----------------

def _request(state, method, path, body=None, data=None, content_type="application/json"):
    url = state.base_url + path
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    payload = None
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer {}".format(state.key))
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            # 服务端已受理（含业务拒绝 4xx/5xx），重试只会重复副作用，不重试
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("HTTP {} {}: {}".format(exc.code, path, detail)) from exc
        except urllib.error.URLError as exc:
            # 只重试「连接未建立」类失败（拒连/超时）：请求未送达服务端，
            # 重试无重复副作用；已发出的失败（含 reset）一律不重试
            retryable = isinstance(
                exc.reason, (ConnectionRefusedError, socket.timeout, TimeoutError)
            )
            if attempt < RETRIES and retryable:
                time.sleep(2 ** attempt)
                continue
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


def _meta_body(state, nodeid):
    """marker 元数据 → 平台用例字段（仅含显式给出的键）"""
    meta = state.meta.get(nodeid) or {}
    body = {}
    for src, dst in _META_FIELDS:
        if meta.get(src):
            body[dst] = meta[src]
    return body


def _resolve_case_id(state, nodeid):
    """映射三层：显式 marker > --bcode-sync 查/建/更新 > 0（仅 external_key）"""
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
    meta = _meta_body(state, nodeid)
    if found and found.get("list"):
        case_id = found["list"][0]["id"]
        if meta:
            # 元数据以测试代码为唯一事实源：sync 重跑即回写（幂等 upsert 语义）
            _request(
                state, "PUT", "/v1/test-cases/{}".format(case_id),
                dict(meta, externalKey=nodeid),
            )
        return case_id
    body = {
        "title": meta.get("title") or nodeid,
        "module": meta.get("module") or "pytest",
        "priority": meta.get("priority") or "P2",
        "externalKey": nodeid,
    }
    for key in ("category", "preconditions", "expectedResult"):
        if meta.get(key):
            body[key] = meta[key]
    created = _request(
        state, "POST", "/v1/projects/{}/test-cases".format(state.project), body
    )
    return created.get("id", 0)


def _case_title(state, nodeid):
    meta = state.meta.get(nodeid) or {}
    if meta.get("title"):
        return meta["title"]
    return nodeid.rsplit("::", 1)[-1]


def _idempotency_key(git_sha, started_at):
    """sha256(git_sha + startedAt + hostname)：同一次执行的网络重发收敛为同一条 run"""
    raw = "{}|{}|{}".format(git_sha, started_at, socket.gethostname())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_payload(state):
    cases = []
    for nodeid, entry in state.results.items():
        cases.append(
            {
                "testCaseId": _resolve_case_id(state, nodeid),
                "externalKey": nodeid,
                "title": _case_title(state, nodeid),
                "status": entry["status"],
                "durationMs": entry["duration_ms"],
                "message": entry["message"],
            }
        )
    finished = time.time()
    started = state.start_ts or finished
    started_at = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S")
    git_sha = _git("rev-parse", "HEAD")
    return {
        "source": state.source,
        "branch": state.branch_override or _git("rev-parse", "--abbrev-ref", "HEAD"),
        "gitSha": git_sha,
        "env": state.env,
        "startedAt": started_at,
        "finishedAt": datetime.fromtimestamp(finished).strftime("%Y-%m-%d %H:%M:%S"),
        "durationMs": int((finished - started) * 1000),
        # 同键重发由平台收敛（返回既有 run + duplicate 标记），网络重试安全
        "idempotencyKey": _idempotency_key(git_sha, started_at),
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
    return created.get("id", 0), bool(created.get("duplicate"))


def _warn_unmapped(state):
    if state.sync:
        return
    unmapped = [n for n in state.results if n not in state.case_ids]
    if unmapped:
        print(
            "[bcode] 提示：未开 --bcode-sync，{} 条用例仅记录 nodeid（external_key），"
            "未关联平台用例".format(len(unmapped))
        )


# ---------------- 附件回传（失败/错误用例） ----------------

def _screenshot_files(state, nodeid):
    """约定目录扫描：文件名以净化 nodeid 为前缀（sanitized_nodeid*.png）"""
    directory = state.screenshots_dir
    if not directory or not os.path.isdir(directory):
        return []
    stem = re.sub(r'[\\/:*?"<>|]', "_", nodeid)
    return sorted(glob.glob(os.path.join(directory, stem + "*")))


def _upload_attachments(state, run_id):
    """run 上报后回传附件：先查详情拿 externalKey → 用例行 id 映射，再补传"""
    targets = sorted(
        n for n, e in state.results.items() if e["status"] in ("fail", "error")
    )
    if not targets:
        return 0
    pending = {}
    for nodeid in targets:
        paths = list(state.attach.get(nodeid) or [])
        paths += _screenshot_files(state, nodeid)
        # 去重保序（marker 显式路径 + 约定目录可能重叠）
        seen, unique = set(), []
        for p in paths:
            key = os.path.normpath(p)
            if key not in seen and os.path.isfile(key):
                seen.add(key)
                unique.append(p)
        if unique:
            pending[nodeid] = unique
    if not pending:
        return 0
    detail = _request(state, "GET", "/v1/test-runs/{}".format(run_id))
    id_map = {c["externalKey"]: c["id"] for c in detail.get("cases") or []}
    count = 0
    for nodeid, paths in pending.items():
        entity_id = id_map.get(nodeid)
        if not entity_id:
            continue
        for path in paths:
            _upload_attachment(state, path, entity_id)
            count += 1
    return count


def _upload_attachment(state, path, entity_id):
    """multipart 上传：POST /v1/attachments/upload（entityType=test_run_case）"""
    boundary = "----bcode" + hashlib.sha1(
        "{}{}".format(time.time(), path).encode("utf-8")
    ).hexdigest()
    with open(path, "rb") as fh:
        content = fh.read()
    parts = []
    for name, value in (("entityType", "test_run_case"), ("entityId", str(entity_id))):
        parts.append(
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                boundary, name, value
            )
        )
    parts.append(
        "--{}\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"{}\"\r\nContent-Type: application/octet-stream\r\n\r\n".format(
            boundary, os.path.basename(path).replace('"', "_")
        )
    )
    body = "".join(parts).encode("utf-8") + content + (
        "\r\n--{}--\r\n".format(boundary)
    ).encode("utf-8")
    return _request(
        state, "POST", "/v1/attachments/upload", data=body,
        content_type="multipart/form-data; boundary={}".format(boundary),
    )


# ---------------- CI 输出（GitHub Actions） ----------------

def _ci_output(state, run_id, duplicate):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    for entry in state.results.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    total = sum(counts.values()) or 1
    for nodeid in sorted(state.results):
        entry = state.results[nodeid]
        if entry["status"] not in ("fail", "error"):
            continue
        lines = entry["message"] or entry["status"]
        first = lines.splitlines()[0][:180] if lines else entry["status"]
        # workflow command 转义：% 是保留字符，换行折叠为空格
        esc = lambda s: s.replace("%", "%25").replace("\r", " ").replace("\n", " ")  # noqa: E731
        print("::error title={}::{}".format(esc(nodeid), esc(first)))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    web = state.base_url[:-4] if state.base_url.endswith("/api") else state.base_url
    rows = [
        "## ByteCode 测试上报",
        "",
        "- 执行记录：run #{}（项目 {}{}）".format(
            run_id, state.project, "，幂等重发命中" if duplicate else ""
        ),
        "- 结果：pass {} / fail {} / error {} / skip {}（通过率 {:.0%}）".format(
            counts["pass"], counts["fail"], counts["error"], counts["skip"],
            counts["pass"] / total,
        ),
        "- 平台：{}/project/{}/test-runs".format(web, state.project),
        "",
    ]
    fails = [(n, e) for n, e in sorted(state.results.items())
             if e["status"] in ("fail", "error")]
    if fails:
        rows += ["| 失败用例 | 状态 | 耗时(ms) |", "| --- | --- | --- |"]
        rows += [
            "| `{}` | {} | {} |".format(n, e["status"], e["duration_ms"])
            for n, e in fails
        ]
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")
