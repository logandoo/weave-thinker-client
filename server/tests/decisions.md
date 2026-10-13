
## D-38 | 2026-09-14 | D3 快路径维持默认关 + 名称安全守卫
trigger: I6 — 残余收口时重测 D3：per-user 批次口径 7→6（-14.3%），早前「0%」为全局批次口径错误；扫描同时发现 low=0.06 会把「每日任务记录（9月1日 vs 9月3日）」模板近邻无 LLM 误合
options: (a) 维持默认关 + 加名称安全守卫（数字/日期不同强制 LLM） (b) 提高 low 到 0.06 并开启
chosen: (a)
why: (b) 有可证信息损失（模板型不同实体）；(a) 下收益 14.3% 且零误合风险，默认关保持帕累托；数据与逐对 safe 标注见 tests/d3_merge_shadow_sweep_20260914.log
revisit-if: 候选对规模上升（单用户 ≥10 对）或开启后评测显示压缩保留率 ≥ 基线

## D-39 | 2026-09-14 | E3 listwise 接入任务装配点（仍不接热路径）
trigger: I1 — 残余项「E3 未接入装配点」；计划原文要求 deathmatch/agent_worker 装配点 1 次调用
options: (a) 接入 E4 任务记忆装配点（门控 + None/异常回退），chat/voice 热路径零改动 (b) 同时接入 chat 热路径
chosen: (a)
why: 真实调用 22.8s（远超热路径性能预算约束 12）；任务装配点延迟不敏感且已有 fail-open 回退
revisit-if: 出现低延迟 listwise 路由或装配点改异步

- D-86 | trigger: 用户反馈（2026-09-19）——「pdf 预览高度太低无法浏览」「docx 无滚动条」| options: A) 保留原生 PDF iframe（仅修高度）B) 换 pdfjs 单页 + 翻页按钮 C) pdfjs 连续多页懒渲染（虚拟滚动 + 页码跟随 + 适宽/缩放）| chosen: C | why: 原生 PDF 插件在 headless/Android WebView 不渲染（本产品有 Android WebView 壳）；单页模型无文档级滚动（用户两条反馈同根因）；pdfjs 与 dsh 客户端一致且 canvas 可截图验证 | revisit-if: 超长 PDF（>200 页)内存/渲染开销成为问题（届时加窗口化或降采样）
