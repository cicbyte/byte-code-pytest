# Changelog

本项目的所有显著变更记录在此文件中。提交信息遵循
[Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，
历史段落可随时用 `git cliff -o CHANGELOG.md` 重新生成。

## [Unreleased]

### Added

- `--bcode` 一族参数：session 结束把整批执行结果上报 ByteCode 平台（test_runs）
- 三层用例映射：显式 `@pytest.mark.bytecode(case=N)`、`--bcode-sync` 按 external_key 自动同步、默认仅记录
- 离线模式 `--bcode-dump`（结果落盘 JSON，`bcode test --upload` 补传）与 `--bcode-strict`
- 一键发版链：`Tag Release` 手动触发，git-cliff 推导版本 + bump + 打 tag + 显式 dispatch 发布（PyPI trusted publishing + GitHub Release）
