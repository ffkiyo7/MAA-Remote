# Router 意图识别回归用例

`router_cases.jsonl`：每行一个 `{input, expected}`。改动 `prompts/router.system.md` 或 `schemas/task_plan.schema.json` 后跑回归，防止意图识别质量回退。

## 匹配规则（部分匹配，非全等）

- `expected` 里出现的字段**必须匹配**；未出现的字段**不校验**（允许模型补默认值）。
  例：`{"action":"ask_stage_selection"}` 只校验 action。
- `note` 字段是自由文本，**永远不参与比对**。
- 对象字段递归按上述规则匹配（如 `fight.stone=0` 必须命中，`fight.times` 未写则不查）。
- 每个输出都必须先通过 `task_plan.schema.json` 校验；schema 不过直接算失败。

## 覆盖的意图类别

跑日常 · 指定关卡+次数 · 关闭指定子任务 · 只做部分子任务 · 揉揉乐默认 · 明确碎石/囤药 · 活动关卡未指定(ask_stage_selection) · 活动关卡已指定(run) · 超范围(reject) · 意图不清(clarify)。

> 落地时补一个跑 evals 的脚本（`maa_remote/` 里或独立 `scripts/eval_router.py`），调 DeepSeek 逐条比对。开发期也可用 Claude Code 辅助扩充用例。
