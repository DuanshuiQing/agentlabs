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

**6. `git push` 报 `couldn't create signal pipe, Win32 error 5`（已修复）**

- 症状：`git ls-remote` / `git push` 失败，报错来自 `C:\Program Files\Git\usr\bin\ssh.exe: fatal error - couldn't create signal pipe, Win32 error 5`
- 原因：git 默认调用**它自带的 MSYS 版 ssh**，而 MSYS 程序要靠创建命名管道做信号模拟；**DSH 沙箱禁止创建命名管道**。手工跑 `ssh` 之所以正常，是因为那是 Windows 原生 OpenSSH（`System32`），跟它不是同一个程序
- 修复：让本仓库改用原生 OpenSSH（`git config` 是本地写入，不需要批准）

  ```powershell
  git config --local core.sshCommand 'C:/Windows/System32/OpenSSH/ssh.exe'
  ```

- ⚠️ 这是**仓库级**配置。新建另一个仓库要重设一遍，否则同样的报错会回来；想一劳永逸就写进全局配置（属工作区外，需批准）

**7. known_hosts 的 `update_known_hosts: Permission denied`（已修复）**

- 症状：`ssh -T git@github.com` 认证成功，但额外刷一条 `update_known_hosts: hostfile_replace_entries failed ... Permission denied`
- 原因：OpenSSH 默认 `UpdateHostKeys yes`，会把服务器通告的**其它**主机密钥类型学习并写回 `known_hosts`，而这个写入被沙箱挡住
- 修复：`~/.ssh/config` 里对 github.com 设 `UpdateHostKeys no`。**不影响** known_hosts 中对 Ed25519 主机密钥的严格校验

**8. 在 DSH 会话里跑 `pip install` 必失败（重要，影响 G1）**

- 症状：`OSError: [Errno 13] Permission denied: '...\pip-unpack-XXXX\openai-3.28.0-py3-none-any.whl.metadata'`，退出码 1
- 已排除的可能：**不是**临时目录位置问题（把 `TEMP` 指到工作区内同样失败）；**不是** `--dry-run/--report` 特有（普通 install 一样失败）；**不是**网络（`pip index versions openai` 这类只访问 JSON API 的命令正常，退出码 0）
- 结论：**是 DSH 的文件沙箱导致的** —— 同一条命令在不受限模式下**退出码 0，正常装成**
- **对策：G1 的 pip 步骤必须在你自己的 PowerShell 窗口里跑**，不要在 DSH 会话里跑。若确实要我代跑，需批准一次不受限执行
- 副作用：pip 失败时会在目标位置留下连 `git status` 都打不开的临时目录，只能在不受限模式下删除

**9. `curl.exe` 无法访问 HTTPS（会误导网络排查）**

- 症状：`curl.exe https://pypi.org/simple/` 返回 `HTTP=000`，`-v` 显示 `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030e)`
- 原因：curl 的 Windows TLS 后端取不到凭据。**只有 curl 受影响**
- ⚠️ **这绝不代表网络不通**：同一时刻 DNS 解析正常、443 端口 TCP 连通、`pip` 直连官方 PyPI 正常
- 排查网络请用 `pip` / 浏览器 / `ssh`，**不要用 curl 下结论**

**10. pip 把临时目录拉在工作区根目录（已定位并给出修法）**

- 症状：一次 `pip install` 之后，`D:\Agent_test` 根目录多出十几个 `pip-unpack-*` / `pip-install-*` / `pip-ephem-wheel-cache-*` 目录，**而且在沙箱下删不掉**（`对路径…的访问被拒绝`）
- 机制：Python `tempfile` 的候选临时目录依次是 `TMPDIR` → `TEMP` → `TMP` → `c:\temp` → `c:\tmp` → `\temp` → `\tmp` → **`os.curdir`（当前工作目录）**。前面全都写不进去时，它会**退回当前工作目录**，于是 pip 的临时文件落在了工作区
- 实测：
  - 沙箱内写 `%LOCALAPPDATA%\Temp` **被拒**（`PermissionError: [Errno 13]`）
  - 沙箱内 `tempfile.gettempdir()` 被 DSH 重定向为 `%LOCALAPPDATA%\Temp\dsh-<随机>`
  - ⚠️ **但那个目录并不能真的当临时区用**（第 7 步实测，修正原先的"专属可写临时区"说法）：
    `tempfile.TemporaryDirectory()` 在里面 `mkdir` **成功**，紧接着往里写文件就被拒
    （`PermissionError: [Errno 13]`），退出时 `cleanup()` 也失败（`shutil` 报 `WinError 5`，
    删不掉自己刚建的目录）。**结论：临时目录一律放在工作区内**，不要指望 DSH 的 Temp。
    `mini-agent/runlog.py` 的自检就是为此把临时目录改成 `logs/_selftest/`
  - 强制 `TMPDIR` 后 `gettempdir()` 变成指定目录，**根目录 0 新增垃圾**（已验证）
- **修法**：跑 pip 前把临时目录钉在 `.tmp`
  ```powershell
  $env:TMPDIR = 'D:\Agent_test\.tmp'
  New-Item -ItemType Directory -Force -Path $env:TMPDIR | Out-Null
  ```
  `.tmp/` 已加入 `.gitignore`
- ⚠️ **沙箱进程创建的目录在沙箱下删不掉**（`pip-*`、`.tmp\probe-*` 都验证过）；普通临时**文件**可以删。清目录要么用不受限模式，要么在**你自己的 PowerShell 窗口**里删（那里没有沙箱）
- 本次已完成：`openai 3.28.0`、`python-dotenv 1.2.4`（含 pydantic 2.14.0 / httpx2 2.13.1 / anyio 4.15.1），走清华镜像，镜像已固化进 `.venv\pip.ini`

**11. git 钩子在**本会话的沙箱里跑不起来**（`sh.exe` 无法创建信号管道）**

- 症状：`git commit` 时钩子被触发，但立刻失败——
  `sh (35560) C:\Program Files\Git\usr\bin\sh.exe: *** fatal error - couldn't create signal pipe, Win32 error 5`
- 根因：和上面第 6 条（git 自带 MSYS ssh 无法创建信号管道）**是同一个故障**。git 的钩子一律交给自带的 `sh.exe` 执行，而沙箱禁止创建命名管道
- 后果：**在本会话里，只要配了 `core.hooksPath`，每一次 `git commit` 都会失败**（钩子非零退出 = 提交中止）
- 修法：本会话提交时用 `git commit --no-verify`，**并把检查串进命令本身**，而不是靠记性：
  ```powershell
  .\mini-agent\.venv\Scripts\python.exe mini-agent\check_data.py; if ($LASTEXITCODE -eq 0) { git commit --no-verify -F .tmp\msg.txt }
  ```
- ⚠️ **在你自己（沙箱外）的 PowerShell 窗口里，钩子是可以正常工作的** —— 那里 `sh.exe` 不受限。所以 `.githooks/pre-commit` 要留着，它保护的是你的提交
- 这条钩子的来历：第 9 步真实发生过一次事故 —— 一次刻意破坏 fixture 的实验在 `roster.txt` 留下"预算 3800 元"，随后一条 `git add -A` 把它一起提交了（`490e7da`），T2 的答案不再唯一。而 `check_data.py` 当时就能查出来，**只是没人记得跑**

## 两条边界（设计，不是故障）

1. **权限**：本会话是 `workspace-write` + 逐次批准。`D:\Agent_test` 内可写，**区外只读**，越界会弹批准。
2. **成本**：context 是预算。长任务用 subagent 把脏活外包，主对话只留结论。
