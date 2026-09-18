# 验证报告：preflight-friend-index-deadlock

- 需求：`preflight-friend-index-deadlock`（workflow: hotfix，isolation: current，branch: main）
- 验证模式：`full`（`comet state scale` 判定：tasks 4 > 3）
- 提交基线：`c5d3576` ｜ 被验证提交：`d0ad2d1`
- 验证时间：2026-09-18
- 结论：**通过**（无 CRITICAL / IMPORTANT 问题）

## 摘要

| 维度 | 结果 |
| --- | --- |
| 完整性 | 4/4 任务完成；无 delta spec（`skip_specs: true`） |
| 正确性 | 根因代码已彻底移除；新增 3 个回归测试覆盖三类触发输入，RED→GREEN 证据齐全 |
| 一致性 | 实现符合 `design.md` 的单一方案；未发现规格/设计漂移 |

## 完整验证检查项

| # | 检查项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | tasks.md 全部完成 | PASS | `tasks.md` 4/4 为 `[x]` |
| 2 | 实现符合 `design.md` 高层设计决策 | PASS | 按 design.md「单一方案」删除预检门禁；未新增索引刷新路径，仅移除挡在既有刷新路径前的门禁 |
| 3 | 符合 Design Doc（`docs/superpowers/specs/`） | N/A | 本 change 走 hotfix 预设，`design_doc: null`，按预设不产出 Design Doc |
| 4 | 能力规格场景全部通过 | N/A | `.openspec.yaml` 设置 `skip_specs: true`；无 delta spec 与能力场景 |
| 5 | proposal.md 目标已满足 | PASS | 「移除硬性门禁」「不再因此暂停账号」「移除失效参数」三项均落地；未改动发送/调度/失败分类语义 |
| 6 | delta spec 与 design doc 无矛盾 | N/A | 无 delta spec，无 Design Doc，不存在可比对的双方 |
| 7 | `docs/superpowers/specs/` 关联设计文档可定位 | N/A | 同上；本 change 无关联设计文档 |

## 轻量检查项（作为补充覆盖）

| # | 检查项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 改动文件与 tasks.md 描述一致 | PASS | `git diff --stat c5d3576...HEAD` = 3 文件 +88/−49：`core/streak_state.py`、`core/tasks.py`、`tests/test_streak_flow_reliability.py`，与 tasks 2/3/4 一一对应 |
| 2 | 编译通过 | PASS | `python -m compileall -q DouYinSparkFlow` 退出码 0（已用 `record-check build` 记录）；干净检出内 `python -m compileall -q .` 退出码 0 |
| 3 | 相关测试通过 | PASS | 干净检出 `d0ad2d1`：231 个测试、**0 失败**；本次 change 所在模块 129 个测试 `OK`（已用 `record-check verify` 记录）；新增 3 个回归测试 `OK` |
| 4 | 无明显安全问题 | PASS | 本次 diff 内 `eval(`/`exec(`/`shell=True` 出现 0 次；唯一命中 `sessionid` 的位置是测试夹具的哑值 `{"name": "sessionid", "value": "x"}`，沿用既有测试写法，非真实凭据 |
| 5 | 最终集成代码审查 | 跳过（已记录原因） | `review_mode: off`（hotfix 预设默认）。跳过原因：本改动为聚焦式删除（3 文件、+88/−49），行为面由 3 个新增回归测试与既有预检/集成测试直接覆盖，未引入新接口、依赖或配置 |
| 6 | 根因消除 | PASS | 全仓库 `require_friend_index` 匹配数为 **0**；`preflight_account` 签名收敛为 `(account, now=None)`；旧门禁分支已删除 |

## RED → GREEN 证据

- 修复前：3 个新增回归测试全部失败，且日志签名与杭州生产完全一致 —— `tasks.py:2206 Account ... failed preflight category=friend_index_stale` 与 `tasks.py:2218 No accounts passed preflight for the scheduled run`。
- 修复后：3 个测试全部通过（`test_scheduled_run_survives_yesterday_friend_index` / `..._missing_friend_index` / `..._incomplete_friend_index`）。
- 覆盖三类触发输入：索引为昨天、索引缺失（全新账号）、`lastScanComplete=False`（扫描中断）。这三类在旧实现下都会永久拒绝运行，因此都属根因分支而非表象。

## 既有测试的处理

- 反转 2 个断言旧门禁行为的既有测试（原 `test_scheduled_preflight_requires_missing_friend_index`、`test_scheduled_preflight_requires_index_for_explicit_browser_account`），改为断言账号通过预检且不被标记 `friend_index_stale`。
  - 说明：这 2 个测试是上一次 streak-flow reliability 改动为门禁写的正向断言，其断言的正是本次修复判定为缺陷的行为；反转是本次修复的必要组成部分，已在此显式记录。
- 更新 1 个受签名变更影响的测试（`test_preflight_accepts_complete_friend_index` 不再传 `require_friend_index`）。
- 保留 `core/tasks.py` 的 `friend_index_stale` 于 `_persist_account_preflight` 的 `recoverable_failure_categories` 中：这是刻意保留，使杭州服务器上已被记录为 `friend_index_stale` 的账号在预检恢复健康后被自动清除历史失败标记。

## Dirty worktree 归因

验证开始时工作区包含**不属于本 change** 的未提交改动：

- 6 个已跟踪文件（`core/friends.py`、`core/streak_state.py`、`login_desktop_server.py`、`webui/app.py`、`webui/static/app.js`、`webui/templates/dashboard.html`）与 3 个未跟踪文件（`core/cookies.py`、`tests/test_account_login_multi_method.py`、`_probe_tmp.py`）。
- 归因结论：全部属于 **Native change `account-login-multi-method`**（其 `phase: build`、`next_action: submit-builder-candidate`）。其中 `core/streak_state.py` 与 `core/tasks.py` 与本次改动同文件，故本次实现采用**选择性暂存**，只把本 change 的 hunk 提交为 `d0ad2d1`，未提交任何 Native 改动。
- 用户已明确决定「Native 那批未提交改动原样保留不动」，因此按 dirty-worktree 协议的归因不确定分支不适用：归属明确且用户已授权保留，验证继续。

### 共享工作区中被观察到的、与本 change 无关的失败

在**共享工作区**（含 Native 在制品）的整仓测试中额外出现 2 个失败，均位于 Native 的 `tests/test_account_login_multi_method.py::LoginDesktopQrPayloadTests`：

- `test_qr_reports_busy_when_page_lock_is_held`（断言英文 `busy`，实际返回中文提示）
- `test_qr_reports_logged_in_state_without_waiting`（期望 200，实际 202）

归因证据：这 2 个失败在**干净检出 `d0ad2d1` 中不存在**（该改动不触及 QR 载荷逻辑）；其成因文件 `login_desktop_server.py` 在本 change 于 13:24 提交之后仍被外部改动（落盘时间 13:25:09，`friends.py` 13:24:24、`webui/app.py` 13:26:15）。因此属 Native 在制品的进行中状态，不是本 change 的缺陷。

### 既存环境限制（两个基线都存在）

- `test_webui_safety.WebUiSafetyTests.test_stale_lock_inspection_does_not_delete_file` 在本机报 `OSError: [WinError 11]`（`webui/ops.py:107` 的 `os.kill(pid, 0)` 在 Windows 上的限制）。
- 该错误在**未做任何改动的 pristine `c5d3576` 基线**上同样存在（基线 228 测试 / 同样这 1 个 error），与本 change 无关。

## 执行过的命令

| 命令 | cwd | 退出码 | 结果 |
| --- | --- | ---: | --- |
| `python -m compileall -q DouYinSparkFlow` | `.` | 0 | 构建/编译证据（build 记录） |
| `python -m unittest discover -s tests -p test_streak_flow_reliability.py` | `DouYinSparkFlow` | 0 | 129 测试 OK（verify 记录） |
| `python -m unittest discover -s tests` | `DouYinSparkFlow`（干净检出 `d0ad2d1`） | 1 | 231 测试、0 失败、1 既有环境 error |
| `python -m unittest discover -s tests -k FriendIndexPreflightDeadlockTests` | `DouYinSparkFlow`（干净检出） | 0 | 3 测试 OK |
| `python -m compileall -q .` | `DouYinSparkFlow`（干净检出） | 0 | 编译通过 |

## 未覆盖 / 已跳过

- spec 场景覆盖率与 delta spec 漂移检测：因 `skip_specs: true` 且无 delta spec，不适用。
- Design Doc 一致性深度比对：hotfix 预设不产出 Design Doc，不适用。
- 自动代码审查：`review_mode: off`，原因见上。
- 未执行真实抖音线上发送或登录；本次为纯代码与测试层验证。

## 结论

全部适用检查项通过，无 CRITICAL / IMPORTANT 问题。根因（好友索引门禁与其生产者构成循环依赖）已被彻底移除，且未新增索引刷新路径、未改动发送与调度语义。可以进入归档。
