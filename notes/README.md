# 笔记

两种笔记都放这里，**一个概念一页**。没写满可以先留空标题——空标题比没有标题更容易被发现。

## 1. 概念笔记（配合 PLAN.md 的各个阶段）

| 文件 | 概念 | 对应 PLAN 章节 | 状态 |
|---|---|---|---|
| `concepts/01-tool.md` | Tool：schema 决定模型怎么用，`description` 决定它用对没用对 | §4.3 | 待写 |
| `concepts/02-agent-loop.md` | Agent loop：决策 → 调工具 → 观察 → 再决策 | §4.1 | 待写 |
| `concepts/03-dispatch-error.md` | 为什么 `dispatch` 必须返回字符串、绝不抛异常 | §4.5 第 4 问 | 待写 |
| `concepts/04-context.md` | Context 预算、历史压缩与摘要 | §4 阶段 1 | 待写 |
| `concepts/05-skill.md` | Skill：渐进披露，代价只在用到时才付 | §4 阶段 2 | 待写 |
| `concepts/06-subagent.md` | Subagent：独立 context 的另一个 agent | — | 待写 |
| `concepts/07-permissions.md` | 沙箱模式与批准策略 | README 边界一节 | 待写 |

## 2. 论文摘要（PLAN.md 阶段 4）

放 `papers/`，每周 2 篇，每篇 1 页，**固定五段**：

1. **问题**：它要解决什么
2. **方法**：怎么做的
3. **实验**：在什么数据上、什么指标、什么结果
4. **局限**：作者自己承认的、以及你看出来的
5. **可复现点**：如果要复现，第一步做什么

**要求：组会上能用中文讲清楚。**讲不清楚的摘要等于没写。

## 写笔记的一条要求

**每个概念都要写清楚「它解决什么问题」，而不只是「它是什么」。**

对照检查：如果你写的是「Tool 是 agent 能调用的函数」，那是"是什么"；如果写的是「Tool 的 `description` 写不清楚，模型就会选错工具——所以它比 system prompt 更影响成功率」，那才是"解决什么问题"。

> 注：`notes/` 只放笔记。**会被 agent 主动加载的知识放 `.dsh/skills/`**，两者不是一回事。
