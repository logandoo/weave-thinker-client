# AUDIT — 安装版默认 skill 全不可见：根因=安装了修复前的旧包（2026-10-12）

> 类型：C4 只读审计（零代码改动）· 复核：fresh-context 独立 VERDICT=CONFIRMED（含 PYZ 字节码反汇编直接实证）

## 结论（一句话）

**不是代码回归——`/Applications/WeaveThinker.app` 是 10-08 09:43 构建的 W10 时代旧包，
其 `_get_skills_dir` 直取 `backend_root/skills`（打包态=用户配置目录，那里没有 skills/），
系统技能列表恒空。修复在 10-08 20:29（W12，746dd5d）就已落地，10-09 的新 DMG 已带修复
并实证过 11 个系统技能。装新包即愈。**

## 症状

安装包版 app 的技能列表里 chatbot 默认 skill（web_search、pptx_manipulation、
docx_manipulation 等 11 个）全部不可见。

## 根因链（每步可复核）

| 步 | 事实 | 证据 |
|---|---|---|
| 1 | 列表链路：`GET /api/skills` → `list_system_skills()` → `_get_skills_dir()` | `server/app/api/skills.py:264-281` · `server/app/tools/skill_tools.py:141-150` |
| 2 | 现行代码（W12 起）：`_get_skills_dir() = config.resolve_resource("skills")`——local（`backend_root/skills`）优先，**bundled（冻结包 `_internal/skills`）兜底** | `skill_tools.py:16-19` · `config.py:264-286` |
| 3 | 打包态 `backend_root` = 用户配置目录（`LOCALSTACK_CONFIG_PATH=$LOCALSTACK_HOME/app/config.toml` 注入） | `desktop/local-stack.cjs:247,264` · `config.py:251-252` |
| 4 | **旧包代码**：`_get_skills_dir() = backend_root / "skills"`（无兜底） | `git show 34ecd48`/`665c0cc:server/app/tools/skill_tools.py:16-18`（10-08 09:28/09:44）；**安装包二进制 PYZ 反汇编 `co_names=('config','backend_root')` 字节码 `config.backend_root / 'skills'`** |
| 5 | 旧包构建时点=10-08 **09:43**，早于修复提交 746dd5d（10-08 **20:29**，改 `resolve_resource("skills")`，提交语录「打包侧车实测列表从 0 系统技能 → 11 个」） | 侧车二进制 mtime `Oct 8 09:43` + sha256 `2a7945e1…` ≠ 树内 `f78fe7bb…`（10-09 19:59） |
| 6 | 打包布局本身无恙：安装包 `_internal/skills` **11/11 SKILL.md 齐全** | `find /Applications/WeaveThinker.app/.../skills -name SKILL.md | wc -l = 11` |
| 7 | 数据目录无影子：`~/Library/Application Support/weave-thinker-client/localstack/app/` 仅 5 个 config 文件，**无 skills/ 目录**（排除 local 遮蔽 bundled） | 目录实况 ls -la |
| 8 | 新包已验证：W15（10-09 19:59）DMG sha256 `6223edd2…`（= `dist_installer/SHA256SUMS.txt`）构建时 scratch 实证 `GET /api/skills` 200 = **11 系统技能含 vibeweaver、块标量解析正常** | `tests/installer/w15_skills_check.json` · `w15_scratch_sidecar_check.txt` · verification_log W15 块 |

## 备择因排除（独立复核逐项核销）

| 备择 | 排除依据 |
|---|---|
| `[agent.skills]` 配置过滤 | 仅 `force_inject`/`force_inject_max_chars`（system prompt 注入），不参与列表过滤 |
| frontmatter 解析失败 | 空列表发生在 `skills_dir.exists()` 判定之前；同解析器已验证 11 个全可解析（解析失败是掉单项不是全空） |
| 鉴权 | 未登录是 401 而非空系统列表；且用户技能会同时消失 |
| 前端过滤 | `SkillsPanel.vue:74-85` 显式渲染 `source==='system'`（带徽标） |
| 打包漏收 skills | `build_sidecar.sh:27 --add-data server/skills:skills`；安装包内 11/11 齐 |
| 空 `skills/` 目录遮蔽 bundled（local-first 语义） | 用户数据目录全域 find 无 skills 目录 |

## 修复动作

1. **立即可用**：安装当前 `dist_installer/WeaveThinker-0.1.0-arm64.dmg`（sha256
   `6223edd2292a03c573c442bac485cc2f37fd434e5f0d11382b44bc561e37e1eb`，W15 构建）——
   带 W12 修复，11 个默认 skill 恢复可见。数据无忧：安装版与 dev 壳共享
   `~/Library/Application Support/weave-thinker-client`（Electron userData 名=package.json name），
   重装不丢账号/数据。
2. **若要带上 W16/W16b/W19（设备执行器）与本会话补波**：从当前树重打一个新包
   （`bash script/linux/package_macos.sh`）再装——W15 的 DMG 早于这些波。

## 顺带建议（可选加固，未实施）

- 打包收尾断言：`package_macos.sh` 末尾对产物侧车跑一次 `GET /api/skills` 技能数核验
  （W15 scratch 冒烟已有该手工核验，可固化为脚本门禁）。
- 运行时哨兵：`resolve_resource("skills")` 解析到不存在目录时在启动日志 WARN——
  旧包这类「静默全空」会立刻可见（本次排查的头号干扰就是静默）。

## 剩余观察（不影响结论）

- `tests/installer/dmg_info.txt` 存有过期哈希（`6c3c0e9b…`，更早波产物）——与本次结论无关，属登记件陈旧。

---

## 追加面（同日）：设备执行能力在客户端「看不到」——同一根因（旧包）的又一表现

**问题**：客户端里找不到「与云端对接、让云端直接在本地电脑操作」的能力。

**结论**：该能力（设备执行器）在**当前树已完整实现**（W16/W16b/W19：`DeviceExecPanel.vue`+
系统设置「设备执行」tab · 后端 executor worker WSS 连云端 `/api/device/ws`+白名单16+fs/zip/office
+progress 续期 · API `client_exec` status/pair/reset · `main.py` 接线有守卫测试）。
但**没有任何现成安装包含它**：
- 已装 app（10-08 09:43）早于 W16（10-10 04:15 首个面板提交）→ 前端 asar `DeviceExecPanel|设备执行` **0 命中**；
- 运行中的安装版后端（127.0.0.1:8160）实测：`/`→200（健康）· `/api/skills`→401（路由在/要鉴权）
  · `/api/client-exec/status|pair`→**404（路由不存在）**——干净证明旧包无此 API；
- 现存 `dist_installer` DMG（10-09 19:59=W15）**同样早于 W16** → 装它也没有设备执行。

**获取路径**：
1. 从当前树重打全量包（`bash script/linux/package_macos.sh`——内含 build_sidecar.sh 重建侧车；
   注意树内 `dist_sidecar`（10-09）也是旧的，不能只跑 electron-builder 增量）；
2. 安装后：系统设置 →「设备执行」→ 填云端地址/输入配对码（配对码由云端 UI 生成）→ 状态面板见已连接；
3. **云端前提**：上游会话自述云端两波改动"已就绪（未部署）"——需部署后配对与下发才端到端生效；
4. 数据无忧：安装版与 dev 壳共享 `~/Library/Application Support/weave-thinker-client`，重装不丢数据。

**证据明细**：`app/src/components/SystemSettingsDialog.vue:223`（tab 定义）· `:63,:161-162,:181`（挂载）
· `server/app/api/client_exec.py:20,32,39,97`（路由真身）· 树内 `app/dist`（10-10 06:09）含「设备执行」
（ChatLayout-*.js 2 处）· 安装 asar 0 命中 · 8160 端口 404/401/200 对照 · 侧车构建时间 10-08 09:43 早于
W16 提交 10-10 04:15。

---

## 结案（2026-10-12 13:00）：新全量包已交付

`dist_installer/WeaveThinker-0.1.0-arm64.dmg`（755,413,007B · sha256 `864df8c960691caf9a1627a3e5d1b1d85ac7c44a821132db0998c8b424d21530`）
= 当前树全量重打（含 W12 技能修复 + W16/W16b/W19 设备执行全波）。隔离实例实证：
`GET /api/skills`=11 系统技能 · `GET /api/client-exec/status` 存活（对照旧包 404）。
**安装此包即同时解决本报告两项症状（技能不可见 + 设备执行缺失）。**
