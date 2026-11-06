# 上游待办（handoff）— 2026-10-26 W28 同步波

上游：`/home/user/Documents/DEV/chatbot`（只读红线）· 本波基线 `7bac20d2 → 09679fda`。
口径：仅登记**上游侧应修**项；本仓不代修同步域 in-sync 文件（制造漂移并掩盖上游修复）。

> **更正声明（2026-10-26，A4.9 对抗评审 F1 抓出）**：本文件首版 T1 主张「`backend/config.toml.example`
> 仍带 F7 遗留键，本仓副本与其逐字节 in-sync」——**该前提是事实错误**。分类账实证
> `backend/config.toml.example → category: local-mod`（`docs/upstream/upstream_state.json`），
> 上游该文件在 `09679fda` 只有 10 行 `[sync]` 注释 stub，**根本不含** `code_execution`/`999`；
> 那两个值一直住在**本仓自己的** 672 行 `server/config.toml.example`（本地打包模板，
> `localstack/import_config.py` 的 `_TEMPLATE`）里。故这不是上游缺陷，而是本仓模板滞后
> ——本波已就地修复（见下「本仓已修」节 + `tests/decisions.md` D-118）。

## T1 前端工具标签映射遗留键 `code_execution`（上游自有 · in-sync 文件 · 本仓不代修）

- **事实**：`frontend/src/components/ChatArea.vue:780` `TOOL_NAME_MAP` 以 `code_execution: '代码执行'` 建键，
  而工具注册名是 `execute_code`（`backend/app/tools/code_execution.py:376 name="execute_code"`，本仓同物 `server/app/tools/code_execution.py:376`）。
  `formatToolName` 的回落式 `TOOL_NAME_MAP[name] || name` → `execute_code` 命中不到，**标签回落显示英文原名**。
- **归属**：上游同文件同行同键（`git -C chatbot show 09679fda:frontend/src/components/ChatArea.vue` 第 780 行逐字相同），
  且本仓该件分类为 **in-sync** → 属上游自有缺陷（与 F7 同族：改名时漏掉前端展示映射）。
- **影响**：仅显示层（中文标签回落英文），无功能/安全影响 → 本仓不为此在 in-sync 文件上开 hunk。
- **请求**：`code_execution` 键改 `execute_code`（或两键并存），并考虑由工具清单生成映射表以免再漂。

## T2 上游测试读实例配置实际值（建议改显式 fixture）

- **事实**：`backend/tests/test_loop_governor.py::test_f4_max_consecutive_is_60` 与
  `::test_f7_stall_override_key_is_registered_tool_name` 断言 `get_config()` 的**实际值**，
  即依赖开发者/部署者机器上的 `backend/config.toml`（同步域外、非版本化文件）。
  后果：从 `config.toml.example` 模板新建实例的环境跑上游全量必红这 2 件（本仓 dev 实例首曝）。
- **同型问题已三次复现**：W23 `[agent.skills].force_inject`（D-108）· W27 域外依赖件（D-114）· 本波 F4/F7。
- **请求**：这两条断言改为显式 fixture（`Config(...)` 注入或 `monkeypatch` 配置路径），
  或同时随 F7 修订 `backend/config.toml.example`——把「实例配置已对齐」从测试前提里摘出去。

## 本仓已修（不属上游动作，登记备查）

- `server/config.toml.example`（本仓 local-mod 打包模板）：`max_consecutive_iterations 999→60`（F4）·
  停滞覆盖表键 `code_execution→execute_code`（F7）。RED→GREEN 证据：以未修模板生成配置跑
  `test_f4`/`test_f7` = 2 failed；修后同一夹具 `tests/test_loop_governor.py` = **46 passed**
  （`tests/w28_iter7_example_proof.log`）。打包态（`import_config.py` 以本模板生成实例）随之转正确。
- `server/config.toml`（gitignored dev 实例）：同两值对齐（D-116）。
