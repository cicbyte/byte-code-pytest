"""自测：嵌套 pytest（pytester）+ 桩平台服务器，验证载荷形状、三层映射与增强能力。

不依赖真实平台：桩服务器实现插件用到的端点（上报 / externalKey 查找 /
建用例 / 用例更新 / run 详情 / 附件上传），并记录请求供断言。
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest_plugins = ["pytester"]


class StubPlatform:
    """内存桩平台：记录请求并按规则响应"""

    def __init__(self):
        self.runs = []          # 实际新建的 POST /test-runs body 列表（去重后）
        self.all_run_bodies = []  # 收到的全部上报 body（幂等去重前）
        self.idem = {}          # idempotencyKey -> run_id
        self.created_cases = []
        self.case_updates = []  # [(case_id, PUT body)]
        self.uploads = []       # [{"entityId", "filename", "size"}]
        self.known_cases = []   # 预置用例 [{"id":1,"external_key":...}]
        self.requests = []
        self.fail_runs = False
        self._server = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:{}/api".format(self._server.server_address[1])

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _make_handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                self._raw(code, {"code": 200, "message": "ok", "data": payload})

            def _raw(self, code, shell):
                body = json.dumps(shell).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length)) if length else {}

            def _parse_multipart(self):
                ctype = self.headers.get("Content-Type", "")
                boundary = ctype.split("boundary=")[-1].strip()
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fields, files = {}, {}
                for part in raw.split(("--" + boundary).encode()):
                    part = part.strip(b"\r\n")
                    if not part or part == b"--":
                        continue
                    header, _, value = part.partition(b"\r\n\r\n")
                    m = re.search(rb'name="([^"]+)"', header)
                    if not m:
                        continue
                    name = m.group(1).decode()
                    fm = re.search(rb'filename="([^"]*)"', header)
                    if fm:
                        files[name] = (fm.group(1).decode(), value)
                    else:
                        fields[name] = value.decode()
                return fields, files

            def do_GET(self):
                u = urlparse(self.path)
                stub.requests.append(("GET", self.path))
                auth = self.headers.get("Authorization", "")
                assert auth.startswith("Bearer bc_"), "应携带 Bearer bc_ 认证"
                m = re.search(r"/test-runs/(\d+)$", u.path)
                if m:
                    # run 详情：回显最后一次上报的用例并分配行 id（900+）
                    cases = [
                        {"id": 900 + i, "externalKey": c["externalKey"]}
                        for i, c in enumerate(stub.all_run_bodies[-1]["cases"])
                    ]
                    self._send(200, {"id": int(m.group(1)), "cases": cases})
                    return
                if u.path.endswith("/test-cases"):
                    q = parse_qs(u.query)
                    key = (q.get("externalKey") or [""])[0]
                    hits = [c for c in stub.known_cases if c["external_key"] == key]
                    self._send(200, {"list": hits, "total": len(hits)})
                    return
                self._send(404, {})

            def do_PUT(self):
                u = urlparse(self.path)
                stub.requests.append(("PUT", u.path))
                m = re.search(r"/test-cases/(\d+)$", u.path)
                if m:
                    stub.case_updates.append((int(m.group(1)), self._read_json()))
                    self._send(200, {})
                    return
                self._send(404, {})

            def do_POST(self):
                u = urlparse(self.path)
                stub.requests.append(("POST", u.path))
                if u.path.endswith("/test-runs"):
                    if stub.fail_runs:
                        # 业务拒：壳 code 非 200（只写一次响应）
                        self._raw(200, {"code": 403, "message": "无权限：Agent 未被授予「上报测试执行」能力"})
                        return
                    body = self._read_json()
                    stub.all_run_bodies.append(body)
                    key = body.get("idempotencyKey")
                    if key and key in stub.idem:
                        self._send(200, {"id": stub.idem[key], "duplicate": True})
                        return
                    run_id = 77 if not stub.runs else 77 + len(stub.runs)
                    if key:
                        stub.idem[key] = run_id
                    stub.runs.append(body)
                    self._send(200, {"id": run_id, "duplicate": False})
                    return
                if u.path.endswith("/test-cases"):
                    body = self._read_json()
                    stub.created_cases.append(body)
                    new_id = 500 + len(stub.created_cases)
                    stub.known_cases.append(
                        {"id": new_id, "external_key": body.get("externalKey", "")}
                    )
                    self._send(200, {"id": new_id})
                    return
                if u.path.endswith("/attachments/upload"):
                    fields, files = self._parse_multipart()
                    fname, content = files.get("file", ("", b""))
                    stub.uploads.append({
                        "entityType": fields.get("entityType"),
                        "entityId": int(fields.get("entityId", "0")),
                        "filename": fname,
                        "size": len(content),
                    })
                    self._send(200, {"id": 1000 + len(stub.uploads)})
                    return
                self._send(404, {})

        return Handler


@pytest.fixture
def stub():
    s = StubPlatform().start()
    yield s
    s.stop()


SUITE = """
import pytest

@pytest.mark.bytecode(case=123)
def test_mapped():
    assert True

def test_plain_pass():
    assert True

def test_plain_fail():
    assert 1 == 2

@pytest.mark.skip(reason="later")
def test_skipped():
    pass

@pytest.fixture
def broken():
    raise RuntimeError("fixture boom")

def test_error(broken):
    assert True
"""

SUITE_META = '''
import pytest

def test_with_doc():
    """结账流程正常返回 ok。"""
    assert True

@pytest.mark.bytecode(module="任务", category="API 自动化", priority="P1",
                     pre="已登录", expected="返回 ok")
def test_rich_meta():
    assert True

@pytest.mark.bytecode(case=77)
def test_explicit():
    assert True

@pytest.mark.bytecode(skip_report=True)
def test_secret():
    assert True

def test_smoke_fast():
    assert True
'''


def run_pytester(pytester, stub_url, extra_args=(), rc_ok=True):
    pytester.makepyfile(test_suite=SUITE)
    # 插件经 pytest11 entry point 自动加载（编辑安装），无须 -p 显式指定
    result = pytester.runpytest_subprocess(
        "--bcode",
        "--bcode-url", stub_url,
        "--bcode-key", "bc_test_key",
        "--bcode-project", "1",
        "--bcode-branch", "feature/x",
        *extra_args,
    )
    if rc_ok:
        # 有失败用例时退出码为 1，这是测试集本身的预期
        assert result.ret in (0, 1)
    return result


def test_report_payload_shape(pytester, stub):
    run_pytester(pytester, stub.url)
    assert len(stub.runs) == 1
    run = stub.runs[0]
    assert run["source"] == "pytest"
    assert run["branch"] == "feature/x"
    assert run["startedAt"] and run["finishedAt"]
    assert len(run["startedAt"]) == 19
    # 幂等键：sha256 十六进制，长度受平台 128 上限约束
    assert re.fullmatch(r"[0-9a-f]{64}", run["idempotencyKey"])
    by_key = {c["externalKey"]: c for c in run["cases"]}
    # 状态映射：pass/fail/skip/error 四态齐
    assert by_key["test_suite.py::test_mapped"]["status"] == "pass"
    assert by_key["test_suite.py::test_mapped"]["testCaseId"] == 123  # 显式映射
    assert by_key["test_suite.py::test_plain_fail"]["status"] == "fail"
    assert "assert 1 == 2" in by_key["test_suite.py::test_plain_fail"]["message"]
    assert by_key["test_suite.py::test_skipped"]["status"] == "skip"
    assert by_key["test_suite.py::test_error"]["status"] == "error"
    assert "fixture boom" in by_key["test_suite.py::test_error"]["message"]
    # 默认（未开 sync）无映射用例 testCaseId=0，仅 external_key 记录
    assert by_key["test_suite.py::test_plain_pass"]["testCaseId"] == 0
    # 标题取 nodeid 尾段
    assert by_key["test_suite.py::test_plain_fail"]["title"] == "test_plain_fail"


def test_sync_creates_cases_idempotently(pytester, stub):
    stub.known_cases.append(
        {"id": 9, "external_key": "test_suite.py::test_plain_pass"}
    )
    run_pytester(pytester, stub.url, extra_args=("--bcode-sync",))
    run = stub.runs[0]
    by_key = {c["externalKey"]: c for c in run["cases"]}
    # 已存在 → 复用 9；不存在 → 新建（500+）；显式映射不受影响
    assert by_key["test_suite.py::test_plain_pass"]["testCaseId"] == 9
    assert by_key["test_suite.py::test_mapped"]["testCaseId"] == 123
    created_keys = {c["externalKey"] for c in stub.created_cases}
    assert "test_suite.py::test_plain_pass" not in created_keys
    assert "test_suite.py::test_plain_fail" in created_keys


def test_missing_config_fails_loud(pytester):
    pytester.makepyfile("def test_x():\n    assert True\n")
    result = pytester.runpytest_subprocess("--bcode")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*缺少上报配置*"])


def test_upload_failure_soft_by_default(pytester, stub):
    stub.fail_runs = True
    result = run_pytester(pytester, stub.url)
    # fnmatch 中 [] 是字符类语法，避免在模式里用方括号
    result.stdout.fnmatch_lines(["*上报失败*能力*"])


def test_upload_failure_strict_fails(pytester, stub):
    stub.fail_runs = True
    result = run_pytester(pytester, stub.url, extra_args=("--bcode-strict",), rc_ok=False)
    # 契约是非零退出（测试集本身有失败时 pytest 仍按 1 收口，区分无必要）
    assert result.ret != 0


def test_dump_mode_writes_offline_json(pytester, tmp_path):
    """离线模式：无 url/key/project 也能跑，结果落盘为 CLI 可补传的格式壳"""
    dump = tmp_path / "run.json"
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess(
        "--bcode",
        "--bcode-dump", str(dump),
    )
    assert result.ret in (0, 1)
    result.stdout.fnmatch_lines(["*已离线落盘*补传*"])
    doc = json.loads(dump.read_text(encoding="utf-8"))
    assert doc["format"] == "bcode-test-run" and doc["version"] == 1
    payload = doc["payload"]
    assert payload["source"] == "pytest"
    assert len(payload["cases"]) == 5
    by_key = {c["externalKey"]: c for c in payload["cases"]}
    assert by_key["test_suite.py::test_plain_fail"]["status"] == "fail"
    # dump 模式不做映射查询（无平台），显式 marker 映射仍保留
    assert by_key["test_suite.py::test_mapped"]["testCaseId"] == 123
    assert by_key["test_suite.py::test_plain_pass"]["testCaseId"] == 0
    assert len(payload["startedAt"]) == 19


def test_metadata_sync_create_and_update(pytester, stub):
    """T1-1：docstring/marker 元数据随 sync 落库，已存在用例 PUT 回写"""
    stub.known_cases.append(
        {"id": 9, "external_key": "test_meta.py::test_with_doc"}
    )
    pytester.makepyfile(test_meta=SUITE_META)
    result = pytester.runpytest_subprocess(
        "--bcode", "--bcode-sync",
        "--bcode-url", stub.url, "--bcode-key", "bc_test_key",
        "--bcode-project", "1",
    )
    assert result.ret == 0
    run = stub.runs[0]
    by_key = {c["externalKey"]: c for c in run["cases"]}
    # skip_report 用例完全不上报
    assert set(by_key) == {
        "test_meta.py::test_with_doc", "test_meta.py::test_rich_meta",
        "test_meta.py::test_explicit", "test_meta.py::test_smoke_fast",
    }
    # 已存在 → PUT 回写 docstring 标题；新建 → marker 全字段进创建体
    assert stub.case_updates == [
        (9, {"title": "结账流程正常返回 ok。", "externalKey": "test_meta.py::test_with_doc"})
    ]
    created = {c["externalKey"]: c for c in stub.created_cases}
    rich = created["test_meta.py::test_rich_meta"]
    assert rich["module"] == "任务"
    assert rich["category"] == "API 自动化"
    assert rich["priority"] == "P1"
    assert rich["preconditions"] == "已登录"
    assert rich["expectedResult"] == "返回 ok"
    # 显式映射不走 sync 查建，也不触发元数据 PUT
    assert by_key["test_meta.py::test_explicit"]["testCaseId"] == 77
    assert all(cid != 77 for cid, _ in stub.case_updates)
    # 上报载荷的标题也走 docstring 兜底链
    assert by_key["test_meta.py::test_with_doc"]["title"] == "结账流程正常返回 ok。"


def test_unmapped_warning_without_sync(pytester, stub):
    """T1-2：未开 sync 且存在未映射用例时给一行提示；开了则无"""
    result = run_pytester(pytester, stub.url)
    result.stdout.fnmatch_lines(["*未开 --bcode-sync*未关联平台用例*"])
    result2 = run_pytester(pytester, stub.url, extra_args=("--bcode-sync",))
    result2.stdout.no_fnmatch_line("*未关联平台用例*")


def test_exclude_patterns(pytester, stub):
    """T1-3：--bcode-exclude fnmatch 排除 + skip_report 标记排除"""
    pytester.makepyfile(test_meta=SUITE_META)
    result = pytester.runpytest_subprocess(
        "--bcode", "--bcode-exclude", "*test_smoke*",
        "--bcode-url", stub.url, "--bcode-key", "bc_test_key",
        "--bcode-project", "1",
    )
    assert result.ret == 0
    by_key = {c["externalKey"]: c for c in stub.runs[0]["cases"]}
    # skip_report（test_secret）与 exclude（test_smoke_fast）都不在载荷
    assert set(by_key) == {
        "test_meta.py::test_with_doc", "test_meta.py::test_rich_meta",
        "test_meta.py::test_explicit",
    }


def test_retry_on_connection_refused(pytester, stub):
    """T1-5：连接未建立类失败指数退避重试，服务端只收到一次上报"""
    pytester.makeconftest("""
        import urllib.error
        import urllib.request

        _real = urllib.request.urlopen
        _calls = {"n": 0}

        def flaky(req, *args, **kwargs):
            if req.get_method() == "POST" and req.full_url.endswith("/test-runs") \\
                    and _calls["n"] < 2:
                _calls["n"] += 1
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            return _real(req, *args, **kwargs)

        urllib.request.urlopen = flaky
    """)
    result = run_pytester(pytester, stub.url)
    result.stdout.fnmatch_lines(["*已上报执行记录*"])
    result.stdout.no_fnmatch_line("*上报失败*")
    assert len(stub.runs) == 1


def test_idempotency_key_dedupes(pytester, stub):
    """T2-2：同键重发返回既有 run（duplicate），不产生第二条记录"""
    # 直接 patch 幂等键生成（经模块全局调用，patch 生效；替换 sessionfinish
    # 钩子属性无效——pytest 注册时已捕获原函数引用）
    pytester.makeconftest("""
        import byte_code_pytest.plugin as plugin

        plugin._idempotency_key = lambda git_sha, started_at: "f" * 64
    """)
    pytester.makepyfile(test_suite=SUITE)
    args = ("--bcode", "--bcode-url", stub.url,
            "--bcode-key", "bc_test_key", "--bcode-project", "1")
    first = pytester.runpytest_subprocess(*args)
    second = pytester.runpytest_subprocess(*args)
    first.stdout.fnmatch_lines(["*已上报执行记录 run #77*"])
    second.stdout.fnmatch_lines(["*幂等重发命中*"])
    # 两次请求携带同一幂等键，桩只实际建了一条 run
    assert len(stub.all_run_bodies) == 2
    assert stub.all_run_bodies[0]["idempotencyKey"] == "f" * 64
    assert stub.all_run_bodies[1]["idempotencyKey"] == "f" * 64
    assert len(stub.runs) == 1
    assert len(stub.idem) == 1


def test_attachments_uploaded_after_run(pytester, stub):
    """T2-1：失败用例的约定目录截图 + marker 显式附件，run 上报后回传"""
    shots = pytester.path / "screenshots"
    shots.mkdir()
    (shots / "test_suite.py__test_shot-1.png").write_bytes(b"\x89PNG-fake!")
    manual = pytester.path / "extra"
    manual.mkdir()
    (manual / "manual.log").write_text("boom", encoding="utf-8")
    pytester.makepyfile(test_suite="""
        import pytest

        @pytest.mark.bytecode(attach_on_fail=["extra/manual.log"])
        def test_shot():
            assert 1 == 2

        def test_ok():
            assert True
    """)
    result = pytester.runpytest_subprocess(
        "--bcode", "--bcode-url", stub.url,
        "--bcode-key", "bc_test_key", "--bcode-project", "1",
    )
    result.stdout.fnmatch_lines(["*已回传 2 个失败/错误用例附件*"])
    # 详情接口按 externalKey 顺序分配行 id：test_shot 是第一条 → 900
    assert len(stub.uploads) == 2
    assert {u["entityType"] for u in stub.uploads} == {"test_run_case"}
    assert {u["entityId"] for u in stub.uploads} == {900}
    assert {u["filename"] for u in stub.uploads} == {
        "manual.log", "test_suite.py__test_shot-1.png"
    }
    assert next(u for u in stub.uploads if u["filename"].endswith(".png"))["size"] == 10


def test_ci_output_annotations_and_summary(pytester, stub):
    """T1-4：GitHub Actions 下输出 ::error 注解与 Step Summary 汇总表"""
    pytester.makeconftest("""
        import os

        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["GITHUB_STEP_SUMMARY"] = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "summary.md"
        )
    """)
    result = run_pytester(pytester, stub.url)
    result.stdout.fnmatch_lines(["::error title=*test_plain_fail::*"])
    summary = (pytester.path / "summary.md").read_text(encoding="utf-8")
    assert "## ByteCode 测试上报" in summary
    assert "/project/1/test-runs" in summary  # 深链：去 /api 前缀 + 前端路由
    assert "test_plain_fail" in summary
    assert "test_error" in summary
