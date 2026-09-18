"""自测：嵌套 pytest（pytester）+ 桩平台服务器，验证载荷形状与三层映射。

不依赖真实平台：桩服务器实现插件用到的三个端点（上报 / externalKey 查找 /
建用例），并记录请求供断言。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest_plugins = ["pytester"]


class StubPlatform:
    """内存桩平台：记录请求并按规则响应"""

    def __init__(self):
        self.runs = []        # POST /test-runs 的 body 列表
        self.created_cases = []
        self.known_cases = []  # 预置用例 [{"id":1,"external_key":...}]
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

            def do_GET(self):
                u = urlparse(self.path)
                stub.requests.append(("GET", self.path))
                auth = self.headers.get("Authorization", "")
                assert auth.startswith("Bearer bc_"), "应携带 Bearer bc_ 认证"
                if u.path.endswith("/test-cases"):
                    q = parse_qs(u.query)
                    key = (q.get("externalKey") or [""])[0]
                    hits = [c for c in stub.known_cases if c["external_key"] == key]
                    self._send(200, {"list": hits, "total": len(hits)})
                    return
                self._send(404, {})

            def do_POST(self):
                u = urlparse(self.path)
                stub.requests.append(("POST", u.path))
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                if u.path.endswith("/test-runs"):
                    if stub.fail_runs:
                        # 业务拒：壳 code 非 200（只写一次响应）
                        self._raw(200, {"code": 403, "message": "无权限：Agent 未被授予「上报测试执行」能力"})
                        return
                    stub.runs.append(body)
                    self._send(200, {"id": 77})
                    return
                if u.path.endswith("/test-cases"):
                    stub.created_cases.append(body)
                    new_id = 500 + len(stub.created_cases)
                    stub.known_cases.append(
                        {"id": new_id, "external_key": body.get("externalKey", "")}
                    )
                    self._send(200, {"id": new_id})
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
