# Agent_test

西安电子科技大学计科专业 — agent 与 skill 的学习与实践工作区。

> **学习计划在 [PLAN.md](<D:/Agent_test/PLAN.md>)**。本文件只讲「这个工作区怎么用」和「这台机器的环境事实」，不讲学习路线。

## 目录结构

```
D:\Agent_test\
├── PLAN.md                 # 学习计划：口径、阶段、进度勾选
├── README.md               # 本文件：环境事实、工具链口径、踩坑记录
├── AGENTS.md               # 项目约定 + 学术诚信（待建，DSH 会读进上下文）
├── .dsh/skills/            # skill 落点（DSH 直接加载，rank 100）
├── mini-agent/             # 阶段 0：手写 agent loop（待建）
├── evals/                  # 阶段 1：评测集与结果（待建）
├── mcp-servers/            # 阶段 3：自写 MCP server（待建）
├── labs/                   # 热身与专题实验
│   └── 01-agent-loop/      # 最小 agent loop（假模型，无需 API key）
└── notes/                  # 概念笔记 + 论文摘要
```

## 两条硬规矩

1. **`.dsh/skills/` 里只放 skill**（`<名字>/SKILL.md` 或 `<名字>.md`，**一层深度**）。放 `README.md` 之类的散装 `.md` 会被当成平铺 skill 解析并告警——详见 [.dsh/skills/.gitkeep](<D:/Agent_test/.dsh/skills/.gitkeep>)。
2. **工具的 `dispatch` 一律返回字符串，绝不向外抛异常**。这是整个 agent 设计的要点，理由见 PLAN.md §4.5。

## 为什么 skill 放在 `.dsh/skills/`

skill 的发现根由「最近的含 `.git` 的祖先目录」决定，没有 `.git` 就退回当前工作目录。
本仓库已 `git init`，所以无论从哪个子目录开会话，项目根都稳定解析为 `D:\Agent_test`。

完整的根目录优先级：

| rank | 来源 | 路径 |
|---|---|---|
| 100 | project-dsh | `<项目根>/.dsh/skills` ← **用这个** |
| 200 | project-agents | `<项目根>/.agents/skills` |
| 300 | custom | 配置项 `customSkillDirs` |
| 400 | user-dsh | `C:\Users\Lenovo\.dsh\skills` |
| 500 | user-agents | `C:\Users\Lenovo\.agents\skills` |

## skill 的全部规则（来自 `dsh-skill-filesystem` 源码，非猜测）

- **两种形态**：`<名字>/SKILL.md`（目录包，可带 `scripts/`、`references/`）或 `<名字>.md`（平铺）
- **只扫一层**：`**/SKILL.md` 这种嵌套**故意不被发现**——新人最常踩的坑
- **frontmatter 必填**：`name`（kebab-case）、`description`
- **frontmatter 可选**：`whenToUse`、`metadata`、`user-invocable`、`disable-model-invocation`
- **热加载**：改正文不用重启，下次加载即生效
- **`description` 是胜负手**：它决定模型在什么时机想起这个 skill。写得含糊 = 永不触发

## AGENTS.md 也是真生效的

`dsh-agent-instructions` 的候选文件名是 `["AGENTS.md", "CLAUDE.md"]`，以 `.git` 标记项目根，把**从项目根到会话工作目录**整条链作为持久基线放进**第一次请求**；按字节预算截断，且**没有文件监视器**（外部改动要等下一次成功的读/写才刷新）。

所以根目录的 `AGENTS.md` 不是形式主义——它的内容会真的进入模型上下文。

## 工具链口径（硬规矩）

| 用途 | 用哪个 |
|---|---|
| **本工作区的 agent 项目** | `py -3.12 -m venv .venv`（`Python312` 已装，实测可用） |
| Python 数据处理 / Office 文件 | DSH 内置：`C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（**3.12.14**，带 numpy/pandas/openpyxl/python-docx/python-pptx/Pillow/lxml/XlsxWriter） |
| 系统默认 Python | **3.14.0** — 版本过新、轮子少，**不要用它**做依赖相关的事 |
| Node | `v24.19.0`（`D:\node.js\node.exe`） |
| pnpm | 用 DSH 自带的 `pnpm.mjs`，不装全局 |
| Git | 2.55.0。仓库已初始化，但 **`user.name` / `user.email` 尚未配置**，配之前无法 commit |

## 环境踩坑记录

**1. 工作区读写全失败（2026-10，已修复）**

- 症状：`SetNamedSecurityInfoW failed (Win32 5): grantWrite(D:\Agent_test)`，连列目录都失败
- 原因：账号是该目录所有者、也有写入权，但缺少「取得所有权」项（`WRITE_OWNER=false`），而 DSH 为工作区补授权正需要它 → 自我锁死
- 处理：用内置 skill `diagnose-windows-sandbox-acl` 的官方脚本，先备份原始权限，再补一条「完全控制」，复读验证 `false → true`。文件内容与所有者未改
- 产物：报告与一键回滚脚本在 `D:\Agent_test_acl_recovery\`
- **复发场景**：换盘、换目录、从别处拷贝来的文件夹。直接再用那个 skill，不要自己猜

**2. 中文输出乱码（已修复）**

- 症状：Python 脚本输出在被重定向/被捕获时中文全变乱码
- 原因：Windows 上 `stdout` 不是终端时 Python 退回系统区域编码（GBK），而上游按 UTF-8 解码
- 处理：脚本里加 `sys.stdout.reconfigure(encoding="utf-8")`。终端里若仍乱码，先执行 `chcp 65001`

**3. git 索引 vs 工作区（第一次踩）**

- 一个 0 字节文件从磁盘上消失后，`git status` 显示 `AD`：**索引里是"新增"，工作区是"已删除"**
- 教训：`git rm --cached` 只动索引、**不可能**删工作区文件；`A` 与 `AD` 正好把两者分开显示了

**4. CRLF 警告刷屏（已修复）**

- 症状：每次 `git add` 刷 8 条 `LF will be replaced by CRLF` 警告
- 处理：[.gitattributes](<D:/Agent_test/.gitattributes>) 定死换行规则，现在警告数 0

**5. 这个 shell 其实是 PowerShell 5.1，不是 pwsh 7（重要）**

- 工具描述写的是 `pwsh`，实测 `$PSVersionTable.PSVersion` = **5.1.26100.9444**
- 后果：PS 5.1 **会把空字符串参数整个丢掉**；`$PSNativeCommandArgumentPassing` 这个开关 7.3+ 才有，这里根本不存在
- 真实故障：`ssh-keygen -t ed25519 -f $key -N ''` 报 `Too many arguments` —— `-N` 后面什么都没剩下
- 绕法：空参数写成 `'""'`，即 `ssh-keygen -t ed25519 -f $key -N '""'`
- **别被这条误导**：`ssh-keyscan` 在 OpenSSH 9.5p2 上会报 `unsupported KEX method sntrup761x25519-sha512@openssh.com`，但 `ssh -T git@github.com` 实测传输正常。`ssh-keyscan` 失败 ≠ 连不上
- 该错误还会写进 `known_hosts`：`~/.ssh/known_hosts` 里已含 github.com 的 Ed25519 主机公钥，指纹与 GitHub 官方公布的 `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU` **逐字符一致**

## 两条边界（设计，不是故障）

1. **权限**：本会话是 `workspace-write` + 逐次批准。`D:\Agent_test` 内可写，**区外只读**，越界会弹批准。
2. **成本**：context 是预算。长任务用 subagent 把脏活外包，主对话只留结论。
