# byte-code-pytest

[English](README_EN.md) | 简体中文

> pytest ↔ [ByteCode](https://github.com/cicbyte/byte-code) 平台桥：测试跑完，整批结果自动上报平台执行记录（test_runs），用例与代码三层映射。纯标准库实现，CI 安装即用、零第三方依赖。

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![pytest](https://img.shields.io/badge/pytest-plugin-0A7ED7?logo=pytest)
![dependencies](https://img.shields.io/badge/dependencies-0-success)
![License](https://img.shields.io/badge/license-MIT-green)

<!-- screenshot: 在此处添加平台 Web「项目 → 测试 → 执行记录」页面截图 -->

## 相关仓库

| 仓库 | 说明 |
| --- | --- |
| [byte-code](https://github.com/cicbyte/byte-code) | 平台本体（Go + Vue3）：执行记录 / 测试管理 / 任务流 / Agent 准入——本插件上报的目标 |
| [byte-code-cli](https://github.com/cicbyte/byte-code-cli) | `bcode` CLI：agent 任务工作流；`bcode test --upload` 补传本插件 `--bcode-dump` 离线产物、`--cases --pull\|--push` 用例 YAML 双向同步 |
| [byte-code-pytest](https://github.com/cicbyte/byte-code-pytest) | 本仓库：pytest ↔ 平台桥 |

三仓分工：pytest（本插件）与任意 junit 生态（经 CLI）执行测试 → 平台落执行记录 → 失败一键转缺陷任务 → agent 经 CLI 认领修复 → 重跑验证闭环。


## 目录

- [相关仓库](#相关仓库)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [用例映射（三层）](#用例映射三层)
- [状态映射](#状态映射)
- [幂等、重试与 CI 集成](#幂等重试与-ci-集成)
- [配置](#配置)
- [CI 示例（GitHub Actions）](#ci-示例github-actions)
- [权限说明](#权限说明)
- [开发](#开发)
- [发版](#发版)
- [开源许可证](#开源许可证)

## 功能特性

- **一键批量上报** — session 结束整批上报执行记录，逐用例状态、耗时与失败 traceback 直达平台 Web
- **三层用例映射 + 元数据富化** — 显式 marker / 按 nodeid 自动同步 / 仅记录 external_key；marker 元数据与 docstring 首行随 sync 落库并回写，测试代码是唯一事实源
- **幂等与重试** — 上报携带幂等键，网络重发命中既有 run 不产生重复；连接未建立类失败自动退避重试
- **失败附件回传** — 约定目录截图或 marker 指定文件，run 上报后自动挂到平台用例行
- **CI 友好** — GitHub Actions 自动输出 `::error` 注解与 Step Summary 汇总表；上报失败默认仅告警（`--bcode-strict` 可硬卡）；凭据走环境变量/CI secret 不入库
- **零依赖零开销** — 纯 Python 标准库；不加 `--bcode` 完全不介入测试流程
- **离线补传** — `--bcode-dump` 结果落盘 JSON，联网后 `bcode test --upload` 补传

## 快速开始

```bash
pip install byte-code-pytest

# 平台侧：owner 在项目里接入一个 agent（拿 bc_ key），勾选「上报测试执行」能力
pytest --bcode \
  --bcode-url https://bc.example.com/api \
  --bcode-key "$BCODE_KEY" \
  --bcode-project 42
```

session 结束自动批量上报，控制台输出：

```text
[bcode] 已上报执行记录 run #77（5 用例，项目 42）
```

平台 Web「项目 → 测试 → 执行记录」即可看逐用例状态、耗时与失败 traceback。

## 用例映射（三层）

pytest 用例与平台测试用例的对应关系，按优先级：

1. **显式映射**：`@pytest.mark.bytecode(case=123)` → 直接关联平台用例 #123
2. **自动同步**（`--bcode-sync`）：按 `external_key`（pytest nodeid）查平台用例——不存在则自动创建（标题取 marker `title` 或 docstring 首行）；已存在且 marker 带元数据时回写更新（PUT）；幂等，重跑不重复建
3. **仅记录**（默认）：`test_case_id=0`，nodeid 照记入 `external_key`——历史可追溯，随时可再映射

```python
import pytest

@pytest.mark.bytecode(case=123)  # 显式关联平台用例 #123
def test_checkout():
    assert checkout() == "ok"

@pytest.mark.bytecode(           # 元数据随 --bcode-sync 落库并回写，docstring 首行兜底标题
    module="交易", category="API 自动化", priority="P1",
    pre="已登录", expected="返回 ok",
)
def test_refund():
    """退款按原路退回。"""
    assert refund() == "ok"

@pytest.mark.bytecode(skip_report=True)          # 不上报（批量排除用 --bcode-exclude）
def test_smoke():
    assert True

@pytest.mark.bytecode(attach_on_fail=["a.png"])  # 失败/错误时回传附件
def test_ui():
    assert ui_ok()
```

## 状态映射

| pytest | 平台 |
| --- | --- |
| passed | pass |
| failed | fail |
| setup 失败（fixture/收集错误） | error |
| skipped / skipif / xfail | skip |

失败与错误的 traceback 附加在用例 message 上（客户端截断 4000 字符，服务端兜底 8000）。teardown 失败不改变用例结论，仅追加提示。

## 幂等、重试与 CI 集成

- **幂等键**：每次上报自动携带 `sha256(git_sha + startedAt + hostname)`——网络重发/重试命中同键时平台返回既有 run（响应带 `duplicate` 标记），不产生重复记录
- **重试**：连接未建立类失败（拒连/超时）指数退避自动重试 2 次；服务端已受理的失败（含业务拒绝）不重试，避免重复副作用
- **GitHub Actions**：检测到 `GITHUB_ACTIONS=true` 时自动输出——失败用例 `::error` 注解（PR 内联标红）+ `GITHUB_STEP_SUMMARY` 汇总表（通过率/失败清单/平台深链 `/project/{id}/test-runs`）
- **失败附件回传**：run 上报后，把失败/错误用例的附件挂到平台用例行——`--bcode-screenshots` 目录（默认 `screenshots/`）内文件名以净化 nodeid 为前缀（`tests/test_a.py::test_x` → `tests_test_a.py__test_x*`）自动匹配，或 marker `attach_on_fail` 显式指定；另可选 `--bcode-code all|fail|off`（默认 off）把测试函数源码快照作为 `.py.txt` 附件回传，当次执行的代码上下文直达平台

## 配置

| 参数 | 环境变量 | 说明 |
| --- | --- | --- |
| `--bcode` | — | 启用上报（不加则完全零开销） |
| `--bcode-url` | `BCODE_URL` | 平台 API 地址（含 `/api` 前缀） |
| `--bcode-key` | `BCODE_KEY` | agent `bc_` key。**只放环境变量/CI secret，勿入库** |
| `--bcode-project` | `BCODE_PROJECT` | 平台项目 id |
| `--bcode-source` | — | 来源标识：pytest（默认）/ ci / junit / manual |
| `--bcode-env` | `BCODE_ENV` | 环境标识，默认 local |
| `--bcode-branch` | — | 覆盖 git 分支自动探测 |
| `--bcode-sync` | — | 未映射用例按 nodeid 自动建平台用例（默认关闭）；marker 元数据/docstring 首行随创建写入，已存在则回写更新 |
| `--bcode-exclude <pattern>` | — | fnmatch 排除不上报的用例（按 nodeid，可重复）；单用例粒度用 `@pytest.mark.bytecode(skip_report=True)` |
| `--bcode-screenshots <dir>` | — | 失败/错误用例截图目录（默认 `screenshots`）：文件名以净化 nodeid 为前缀即自动挂到对应用例行 |
| `--bcode-code <scope>` | — | 用例函数源码快照回传：`all`=全部用例 / `fail`=仅失败与错误 / `off`=关闭（默认）；以 `.py.txt` 附件挂到平台用例行，平台侧排障免切仓库 |
| `--bcode-strict` | — | 上报失败时 pytest 非零退出（默认仅告警） |
| `--bcode-dump <path>` | — | 离线模式：结果落盘 JSON 而非直传（无需 url/key/project），后续 `bcode test --upload <path>` 补传 |

分支与 commit 缺省从 git 自动探测；非 git 环境留空不影响上报。

离线/受限网络场景：`pytest --bcode --bcode-dump run.json` 把结果落盘（格式带 `bcode-test-run` 标记壳），之后在能联网的机器 `bcode test --upload run.json` 补传。

## CI 示例（GitHub Actions）

```yaml
- run: pip install byte-code-pytest
- run: pytest --bcode --bcode-source ci --bcode-env ci
  env:
    BCODE_URL: ${{ vars.BCODE_URL }}
    BCODE_KEY: ${{ secrets.BCODE_KEY }}
    BCODE_PROJECT: ${{ vars.BCODE_PROJECT }}
```

## 权限说明

上报走 agent 身份（`bc_` key + Bearer），需要项目 owner 给该 agent 勾选**上报测试执行**（`test_execute`）能力；CI 机器即 agent，复用平台的租约/审计/能力集。未勾选时上报会被明确拒绝（文案含能力名），配合 `--bcode-strict` 可在 CI 硬卡。

## 开发

```bash
python -m venv .venv && .venv/Scripts/pip install -e .[dev]
.venv/Scripts/python -m pytest tests/ -q        # pytester 嵌套跑 + 桩平台服务器
.venv/Scripts/python -m ruff check src tests
```

## 发版

一键发版（对齐 [byte-code](https://github.com/cicbyte/byte-code) 的触发链）：

```bash
gh workflow run "Tag Release" -f version=   # 留空=git-cliff 按提交语义自动推导；也可填 patch/minor/major 或完整版本号
```

`Tag Release` 自动完成：推导版本 → 重新生成 CHANGELOG 并 bump `__version__`（版本唯一来源，pyproject 动态读取）→ 提交并打 tag `vX.Y.Z` → 显式 dispatch [Build & Release](.github/workflows/release.yml)（GITHUB_TOKEN 推的 tag 不会触发 tag 工作流，GitHub 防递归）：校验 tag 与包版本一致 → `build` + `twine check` → PyPI trusted publishing + GitHub Release（cliff 生成发版说明）。

PyPI 侧一次性配置：项目设置里添加 Trusted Publisher（本仓库 + `release.yml` 工作流 + `pypi` 环境），之后发版无需任何 token。

## 开源许可证

[MIT](LICENSE) © 2026 cicbyte
