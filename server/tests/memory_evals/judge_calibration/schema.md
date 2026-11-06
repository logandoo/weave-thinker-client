# judge 校准集条目 schema（W0-3 脚手架）

校准集文件：`cases.jsonl`（每行一个 JSON 对象，UTF-8；**当前不存在**——50 例人工标注是待办，见 `README.md`）。
示例形态见 `cases.sample.jsonl`（3 行全部 `human_verified=false`，只是样例，**不是数据**）。

## 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 校准条目 id，集内唯一（如 `cal-0007`） |
| `query` | string | 用户原始问题（与 M4a 场景的 query 同语义） |
| `gold_answer` | string | 人工定稿的标准答案 |
| `judge_answer` | string | judge（LLM 评审）对该条的原始判语/答案 |
| `gold_labels` | object | 人工标注的维度标签，键=维度名，值=标签值 |
| `judge_labels` | object | judge 给出的同形维度标签（维度名与 `gold_labels` 对齐） |
| `human_verified` | bool | 是否经人工复核确认；**false 的行不计入一致率** |

七个字段全部必填（缺字段 = 数据错误，`calibrate.py` 报错退出 1）。

## 维度约定

维度名自由（例如 `factual_correctness`、`grounded_in_memory`、`honest_missing`），
但同一校准集内应保持稳定：维度集合 = 计入行 `gold_labels` 键的并集。

- 逐维度一致率 =（该维度上 `judge_labels[d] == gold_labels[d]` 的计入行数）/（含该维度的计入行数）。
- judge 在某行缺某维度 = 该 (行,维度) 计不一致。
- `judge_labels` 多出的维度（gold 未标）忽略，不计入。

## 计入 / 排除

- **计入**：`human_verified == true` 的行。
- **排除**：`human_verified == false` 的行（计数显式报告）；空文件/无 `cases.jsonl`（计数显式报告）。
- 当前 50 例人工标注未完成 → 校准集为空 → 一致率不可得。

## 一致率与结论

- `overall_agreement` = Σ(行,维度) 匹配数 / Σ(行,维度) 对数（micro 平均）。
- `calibrate.py --min-agreement 0.8`：
  - `overall_agreement < 0.8` 或无可计入行 → 打印 **「judge 不得进验收」**，exit 2；
  - `overall_agreement ≥ 0.8` → 打印「judge 可进验收」，exit 0。
- 阈值 ≥80% 是 judge 进入验收链路（终局 claim 校验/审计面）的前置条件，未达阈值时 judge 只能作参考信号。
