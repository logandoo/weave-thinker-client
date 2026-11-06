# 上游侧待办（W27 同步波查证，2026-10-24）— chatbot 仓

> 用途：用户携至上游会话执行；归属判定遵循 docs/DECISION_MATRIX_local_vs_upstream.md 四象限。
> 本波已随同步销账的 handoff（勿重复排期）：W20① 配对响应身份双键 · W20② conversation.execution_target
> · W21① 同 device_id 重注册+墓碑闸 · W22/W25③ 设备改名端点（以上全部上游落地，超集实证见
> tests/upstream_merge_audit_w27.md 与 upstream.toml 注释）。

## 项 1（P3，设计一致性）：`_resolve_working_dir` 两分支对越全局根工作区待遇相反

- 位置：`backend/app/tools/terminal.py` `_resolve_working_dir`（带 `working_dir` 分支经 `_caller_root`
  放行可越 `config.workspace_root`；不带时 kwargs `workspace_path` 必须落在 tenant/全局根内否则
  「outside the allowed workspace」直接拒绝）。
- 后果：用户级工作区覆盖（`PUT /api/users/me/workspace`，允许全局根外绝对路径）后，**默认 cwd 的
  终端整类被砖**，显式 working_dir 却可用——同一工作区两种待遇。
- 建议：两分支统一判定口径（均认 `_caller_root`，或均加白名单边界校验）。
- 归属：**仅上游**（终端围栏是云端共享实现；vendor 同步波随上游取）。

## 项 2（P3，防御纵深）：`_caller_root` 无「配置全局根内」硬边界 + 多租户部署要求

- 现状：`_caller_root = kwargs.workspace_path` 无边界校验；W12 用户覆盖的边界仅=绝对路径+非根+可写+
  可选 `[workspace].override_allowed_roots`（**缺省空=不限制**）。
- 多租户共享部署：用户可把覆盖指向同 uid 可写的他人租户目录 → terminal/code-exec 围栏随之移入
  （横向面）；单用户桌面=自伤面。
- 建议：`_caller_root` 分支加 `override_allowed_roots` 同源边界；**部署文档注明多租户必配该白名单**。
- 归属：**双端**（红线 B 面围栏语义；vendor 侧注释已如实标注现状）。

## 项 3（P3，W16 口径扩展）：生效值函数模式外的 raw 哨兵/绕过直判残留

- 位置：`backend/app/tools/{notes,memory}.py`、`services/{workspace_snapshot,browser_interact}.py`
  直读 `args.get("_permission_granted")`/`config.super_admin_bypass`（W16 契约清单 registry/terminal/
  git/process_tool/code_execution 之外）。
- 现状风险：默认设备白名单不含它们且入站剥 `_` 键，**当前不构成远程绕过面**；但若部署扩
  `client_exec.allowed_tools` 至这些工具，raw bypass 将随本地 config 生效（fail-closed 失守）。
- 建议：统一改走 `super_admin_bypass_effective()`/`permission_sentinel_effective(args)` 口径。
- 归属：**双端**（安全收紧=双方同形）。

## 附：环境备注（非上游项）

- 隔离 localstack 的 fresh-venv 安装 `embedded-postgres==18.6.3` 在 tuna 镜像不可得
  （`from versions: none`；官方 pypi.org 可达）——冒烟/隔离实例需 `PIP_INDEX_URL=https://pypi.org/simple`。
