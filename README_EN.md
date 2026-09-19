# byte-code-pytest

English | [简体中文](README.md)

> pytest ↔ [ByteCode](https://github.com/cicbyte/byte-code) platform bridge: when the test session ends, the whole batch of results is uploaded to the platform's test runs, with three-tier test-case mapping. Pure standard library — install and go in CI, zero third-party dependencies.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![pytest](https://img.shields.io/badge/pytest-plugin-0A7ED7?logo=pytest)
![dependencies](https://img.shields.io/badge/dependencies-0-success)
![License](https://img.shields.io/badge/license-MIT-green)

<!-- screenshot: add a screenshot of the platform web page "Project → Tests → Test Runs" here -->

## Related Repositories

| Repository | Description |
| --- | --- |
| [byte-code](https://github.com/cicbyte/byte-code) | The platform itself (Go + Vue3): run records, test management, task flow, agent admission — where this plugin reports to |
| [byte-code-cli](https://github.com/cicbyte/byte-code-cli) | The `bcode` CLI: agent task workflow; `bcode test --upload` back-fills this plugin's `--bcode-dump` artifacts, `--cases --pull\|--push` syncs test cases as YAML |
| [byte-code-pytest](https://github.com/cicbyte/byte-code-pytest) | This repository: the pytest ↔ platform bridge |

How the three fit together: pytest (this plugin) and any junit ecosystem (via the CLI) execute tests → the platform records runs → failures convert to bug tasks in one click → agents claim and fix via the CLI → re-run closes the loop.


## Table of Contents

- [Related Repositories](#related-repositories)
- [Features](#features)
- [Quick Start](#quick-start)
- [Test-Case Mapping (Three Tiers)](#test-case-mapping-three-tiers)
- [Status Mapping](#status-mapping)
- [Idempotency, Retries and CI Integration](#idempotency-retries-and-ci-integration)
- [Configuration](#configuration)
- [CI Example (GitHub Actions)](#ci-example-github-actions)
- [Permissions](#permissions)
- [Development](#development)
- [Release](#release)
- [License](#license)

## Features

- **One-shot batch upload** — the entire session's results are uploaded at once; per-case status, duration and failure tracebacks land directly in the platform web UI
- **Three-tier case mapping + metadata enrichment** — explicit marker / auto-sync by nodeid / record-only external_key; marker metadata and docstring first lines are written back through sync, keeping the test code as the single source of truth
- **Idempotency & retries** — every upload carries an idempotency key, so network re-sends hit the existing run instead of duplicating it; connection-level failures retry with backoff
- **Failure attachments** — screenshots from the conventional directory or files named by the marker are attached to the platform case rows after the run is uploaded
- **CI friendly** — GitHub Actions gets `::error` annotations and a Step Summary automatically; upload failures only warn by default (`--bcode-strict` turns them into a hard gate); credentials live in env vars / CI secrets, never in the repo
- **Zero deps, zero overhead** — pure Python standard library; without `--bcode` the plugin never touches your test run
- **Offline catch-up** — `--bcode-dump` writes results to JSON; upload later with `bcode test --upload` once you are online

## Quick Start

```bash
pip install byte-code-pytest

# Platform side: the owner registers an agent in the project (get a bc_ key)
# and grants it the "report test executions" capability
pytest --bcode \
  --bcode-url https://bc.example.com/api \
  --bcode-key "$BCODE_KEY" \
  --bcode-project 42
```

When the session finishes, results are uploaded in one batch and the console prints:

```text
[bcode] uploaded test run #77 (5 cases, project 42)
```

Open the platform web UI under "Project → Tests → Test Runs" to see per-case status, duration and failure tracebacks.

## Test-Case Mapping (Three Tiers)

How pytest tests correspond to platform test cases, by priority:

1. **Explicit mapping**: `@pytest.mark.bytecode(case=123)` → links directly to platform case #123
2. **Auto-sync** (`--bcode-sync`): looks up platform cases by `external_key` (the pytest nodeid) — creates one when absent (title from the marker `title` or the docstring first line) and writes marker metadata back with PUT when present; idempotent, reruns never duplicate
3. **Record-only** (default): `test_case_id=0`, the nodeid is still stored in `external_key` — history stays traceable and can be mapped later

```python
import pytest

@pytest.mark.bytecode(case=123)  # explicitly link to platform case #123
def test_checkout():
    assert checkout() == "ok"

@pytest.mark.bytecode(           # metadata lands with --bcode-sync; docstring is the title fallback
    module="checkout", category="API automation", priority="P1",
    pre="logged in", expected="returns ok",
)
def test_refund():
    """Refund goes back through the original payment channel."""
    assert refund() == "ok"

@pytest.mark.bytecode(skip_report=True)          # never reported (bulk: --bcode-exclude)
def test_smoke():
    assert True

@pytest.mark.bytecode(attach_on_fail=["a.png"])  # attached on fail/error
def test_ui():
    assert ui_ok()
```

## Status Mapping

| pytest | platform |
| --- | --- |
| passed | pass |
| failed | fail |
| setup failure (fixture/collection error) | error |
| skipped / skipif / xfail | skip |

Failure and error tracebacks are attached to the case message (client truncates at 4000 chars, server caps at 8000). A teardown failure does not change the case verdict; a note is appended.

## Idempotency, Retries and CI Integration

- **Idempotency key**: every upload carries `sha256(git_sha + startedAt + hostname)` — network re-sends and retries hitting the same key return the existing run (response carries a `duplicate` flag), never a duplicate record
- **Retries**: connection-level failures (refused/timeout) retry twice with exponential backoff; failures the server already accepted (including business rejections) are never retried, avoiding duplicated side effects
- **GitHub Actions**: when `GITHUB_ACTIONS=true` is detected the plugin emits `::error` annotations for failed cases plus a `GITHUB_STEP_SUMMARY` table (pass rate / failure list / deep link to `/project/{id}/test-runs`)
- **Failure attachments**: after the run is uploaded, attachments of failed/errored cases are attached to their platform case rows — files under `--bcode-screenshots` (default `screenshots/`) whose names start with the sanitized nodeid (`tests/test_a.py::test_x` → `tests_test_a.py__test_x*`), or files named by the marker `attach_on_fail`

## Configuration

| Option | Env var | Description |
| --- | --- | --- |
| `--bcode` | — | Enable reporting (fully zero-overhead when absent) |
| `--bcode-url` | `BCODE_URL` | Platform API address (including the `/api` prefix) |
| `--bcode-key` | `BCODE_KEY` | Agent `bc_` key. **Keep in env vars / CI secrets only — never commit** |
| `--bcode-project` | `BCODE_PROJECT` | Platform project id |
| `--bcode-source` | — | Source label: pytest (default) / ci / junit / manual |
| `--bcode-env` | `BCODE_ENV` | Environment label, default local |
| `--bcode-branch` | — | Override git branch auto-detection |
| `--bcode-sync` | — | Auto-create platform cases by nodeid for unmapped tests (off by default); marker metadata / docstring first lines are written on create and updated when the case exists |
| `--bcode-exclude <pattern>` | — | fnmatch pattern to exclude cases from reporting (by nodeid, repeatable); per-case alternative: `@pytest.mark.bytecode(skip_report=True)` |
| `--bcode-screenshots <dir>` | — | Screenshot directory for failed/errored cases (default `screenshots`): files prefixed with the sanitized nodeid are attached to the matching case row |
| `--bcode-strict` | — | Non-zero pytest exit when upload fails (warn-only by default) |
| `--bcode-dump <path>` | — | Offline mode: dump results to JSON instead of uploading (no url/key/project needed); catch up later with `bcode test --upload <path>` |

Branch and commit are auto-detected from git by default; in non-git environments they are left empty and uploading still works.

Offline / restricted-network scenarios: `pytest --bcode --bcode-dump run.json` writes results to disk (the payload is wrapped in a `bcode-test-run` envelope); later, on a connected machine, run `bcode test --upload run.json`.

## CI Example (GitHub Actions)

```yaml
- run: pip install byte-code-pytest
- run: pytest --bcode --bcode-source ci --bcode-env ci
  env:
    BCODE_URL: ${{ vars.BCODE_URL }}
    BCODE_KEY: ${{ secrets.BCODE_KEY }}
    BCODE_PROJECT: ${{ vars.BCODE_PROJECT }}
```

## Permissions

Uploads use the agent identity (`bc_` key + Bearer). The project owner must grant the **report test executions** (`test_execute`) capability to the agent; the CI machine is the agent and reuses the platform's lease / audit / capability model. Without the capability, uploads are rejected with a clear message (naming the capability); combine with `--bcode-strict` to hard-gate in CI.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e .[dev]
.venv/Scripts/python -m pytest tests/ -q        # nested pytester runs + stub platform server
.venv/Scripts/python -m ruff check src tests
```

## Release

One-command release (aligned with [byte-code](https://github.com/cicbyte/byte-code)'s trigger chain):

```bash
gh workflow run "Tag Release" -f version=   # empty = derived automatically by git-cliff from commit semantics; or pass patch/minor/major or an explicit version
```

`Tag Release` automatically: derives the version → regenerates the CHANGELOG and bumps `__version__` (the single source of truth, read dynamically by pyproject) → commits and tags `vX.Y.Z` → explicitly dispatches [Build & Release](.github/workflows/release.yml) (tags pushed by GITHUB_TOKEN do not trigger tag workflows — GitHub anti-recursion): verifies the tag matches the package version → `build` + `twine check` → PyPI trusted publishing + GitHub Release (release notes generated by cliff).

One-time PyPI setup: add a Trusted Publisher in the project settings (this repository + the `release.yml` workflow + the `pypi` environment); afterwards releases need no tokens at all.

## License

[MIT](LICENSE) © 2026 cicbyte
