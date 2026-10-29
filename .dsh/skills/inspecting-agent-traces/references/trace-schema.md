# 轨迹字段表（`mini-agent/logs/run-*.jsonl`）

**什么时候读这一页**：只有当 `scripts/trace_summary.py` 的输出不够用、
你要自己写一条新查询时。日常诊断不需要打开它。

一行一个 JSON 对象，UTF-8，LF。`role` 决定这一行是什么。

## `role="run"` —— 头部，整份轨迹第一行

| 字段 | 类型 | 说明 |
|---|---|---|
| `content` | str | 题面（原样交给 agent 的那句话） |
| `run_id` | str | 与文件名同源，如 `run-20261029-215247` |
| `model` | str | 模型名 |
| `max_steps` | int | 这次运行的步数上限 |
| `tools` | list[str] | **这次给了哪些工具**（消融时少一个就在这里） |
| `system` | str \| null | 消融 C 加的系统提示原文；`null` = 没加 |
| `tools_blurred` | dict \| null | 消融 A：工具名 → 模糊后的 description |
| `started_at` | str | ISO 8601，带时区 |

> 这五个字段是**自变量**。没有它们，事后无法判断"这份结果为什么和那份不一样"。

## `role="system"` / `role="user"`

`step` 都是 0。`system` 只在加了系统提示时出现。两者都带着 `content`，是**真正发出去的文本**。

## `role="assistant"` —— 模型的一轮回复

| 字段 | 说明 |
|---|---|
| `step` | 第几轮，从 1 开始 |
| `content` | 模型说的话；请求工具的那一轮**常常是空字符串**，但键一定在 |
| `finish_reason` | `stop` / `tool_calls` / `length` |
| `tool_calls` | `[{id, name, arguments}]`；`arguments` 是 **JSON 字符串**，不是对象 |
| `usage` | `{completion_tokens, prompt_tokens, total_tokens, prompt_cache_hit_tokens, …}` |
| `elapsed_s` | 这次请求的墙钟时间 |

## `role="tool"` —— 一次工具执行的结果

| 字段 | 说明 |
|---|---|
| `content` | 给模型看的观察文本。失败时是 `ERROR: …`（**文本**，不是抛异常） |
| `tool_call_id` / `tool_name` / `arguments` | 与上面那次 `tool_calls` 对应 |
| `is_error` | bool。**模型看不见它**，它只进轨迹 |
| `error_kind` / `error_type` | 失败分类：`unknown_tool` / `bad_args` / `path_escape` / `sensitive_path` / `run_trace` / `timeout` / `tool_error` … |

> `is_error` 与 `error_kind` 是**给复盘用的**：模型的答案里只有那句 ERROR 文本，
> 分类信息只有轨迹里才有。诊断"错在哪一类"必须看这里。

## `role="result"` —— 最后一行

| 字段 | 说明 |
|---|---|
| `ok` | bool。`content` 非空 **且** `finish_reason=="stop"` 才为真 |
| `reason` | `stop`（出口 1）/ `max_steps`（出口 2）/ `error`（崩了） |
| `content` | 最终答案文本。**判据吃的是它** |
| `steps` | 实际用了几轮 |
| `usage_total` | 整条轨迹的 token 合计 |

## 写新查询时的三条坑

1. **`tool_calls` 缺失时是 `null`，不是 `[]`** —— 用 `rec.get("tool_calls") or []`。
2. **`arguments` 是 JSON 字符串**，直接 `in` 判断可以，想取字段得再 `json.loads` 一次。
3. **行序即时间序**，但 `step` 会重复（一轮里有多条 tool 行）。要按"轮"聚合就看 `step`，不要看行号。

## 和结果 JSON 的分工

`evals/results/real-*.json` 是**汇总**（每条任务一行 record、通过率、token 合计）；
轨迹是**过程**（每一步发了什么、观察到什么）。
结果 JSON 里的 `trace` 字段就是这两者之间的外键 —— 从任何一份结果都能指回证据。
