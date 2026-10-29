---
name: inspecting-agent-traces
description: 把 mini-agent 的运行轨迹（logs/run-*.jsonl）读成步骤时间线：每轮调了哪些工具、哪一步出错、每步 token、结论行、停止原因。当你要诊断一次运行为什么失败或为什么这么贵、对比两次运行或两个消融臂、或者要把一次运行的过程讲给别人听时使用。不要手写 grep 去解析这些 JSONL。
---

# 读运行轨迹

一次运行的**过程**只有轨迹里有：模型的答案说得再清楚，也看不到它走错了几步、在哪个工具上卡住、错误属于哪一类。

## 先跑脚本，不要先手写解析

脚本在 `scripts/trace_summary.py`（相对本 skill 的 Base directory，加载时由 `skill_resources` 给出；
在本项目里就是 `.dsh\skills\inspecting-agent-traces`）：

```powershell
mini-agent\.venv\Scripts\python.exe .dsh\skills\inspecting-agent-traces\scripts\trace_summary.py mini-agent\logs\run-20261029-215247.jsonl
```

| 想看什么 | 怎么调 |
|---|---|
| 一份轨迹的完整时间线 | 直接把文件路径传进去 |
| 最近几次 | `mini-agent\logs -n 3` |
| 只看出过错的那几份 | `mini-agent\logs --errors-only` |
| 给别的脚本用 | `--json`（输出每条一份的 dict） |

## 输出里最该看的五行

1. `tools` —— **这次给了哪些工具**。少一个就是消融臂，先确认自变量。
2. `合计 … 步 / … 次工具调用 / … tokens（cached …）` —— 贵在步数还是贵在上下文，cached 越高越便宜。
3. `错误 … 类型=[…]` —— 只有轨迹有 `error_kind`，答案里只有一句 `ERROR:` 文本。
4. `结果 ok=… reason=…` —— `reason=max_steps` 意味着**没答完**，这时的通过率没有意义。
5. `结论` —— 判据真正看的只有这一行。

## 三条纪律

- **判据看的是答案，不是你从轨迹推出来的东西。** 轨迹用来解释"为什么"，不用来改判分；判分口径在 `evals/tasks.py`。
- **`reason=max_steps` 的运行要单独算**，别和正常收尾的运行混在同一个通过率里。
- **重定向到文件时先 `set PYTHONIOENCODING=utf-8`**，否则 Windows 上按 GBK 编码，中文全乱。终端里直接跑没问题。

## 什么时候再往下读

只有要**自己写一条新查询**（脚本不支持的维度），才读 `references/trace-schema.md`（字段表 + 三条坑）。
日常诊断**不需要**打开它。
