# judge 校准台（W0-3 脚手架）

**待办：50 例人工标注。** 由评测负责人（主会话指派，建议熟悉 M4a 评测集与反幻觉约定的两人背靠背标注、分歧仲裁）对 50 条候选条目逐条标定 `gold_answer` 与 `gold_labels`（维度见 `schema.md`：事实正确性、记忆依据、缺证据诚实、陈旧状态处理等），并把 `human_verified` 置为 `true` 后写入 `cases.jsonl`；`cases.sample.jsonl` 只是行形态示例（3 行全部 `human_verified=false`），不是数据。标注完成前校准集为空，`calibrate.py` 判定「judge 不得进验收」。**阈值 ≥80%**（`calibrate.py --min-agreement 0.8`，overall 一致率）才允许 judge 进验收链路；未达阈值时 judge 只能作参考信号，不得用于终局 claim 校验/审计结论。
