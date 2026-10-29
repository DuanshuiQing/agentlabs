"""文件系统小工具 —— 只放"Windows 上会咬人的那几个动作"。

起因是 M0 探针的第二次失败：

    FileExistsError: [WinError 183] 当文件已存在时，无法创建该文件

根因不是路径写错，而是**上一次的沙箱副本根本没删掉**：git 在 Windows 上把
`.git/objects/**` 里的文件设成 **ReadOnly**，而 `shutil.rmtree()` 删只读文件会失败。
`ignore_errors=True` 又把这个失败咽了下去，于是下一次 `copytree` 撞上残留目录 ——
错误出现在"建"的地方，"删"的地方一点声音都没有。这和 README 坑 10 是同一族问题：
**沙箱里的删除失败不会自己说话，你得让它开口。**

所以这里只提供一个动作：删之前先把只读位摘掉。

（顺带一个实测：普通目录 remove 是好的；`sandbox/_probe/tmpdir` 那种一次性目录
能正常删。会咬人的只有带 `.git` 的副本。）
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def _clear_readonly(func, path: str, _exc) -> None:
    """rmtree 的兜底：摘掉只读位再重试一次。

    Python 3.12 起 `rmtree(onexc=...)` 的回调签名是 `(function, path, excinfo)`；
    3.11 及以前是 `onerror=(func, path, exc_info)`。本项目用 3.12，只支持 onexc。
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree_force(path: str | os.PathLike) -> bool:
    """尽力删掉 `path`；返回"删完以后它确实不存在"。

    故意**不抛异常**、也不静默：调用方拿到 False 时可以决定是换用 git 重置，
    还是当场报错。删不干净却继续往下跑，才是真正的危险。
    """
    p = Path(path)
    if not p.exists():
        return True
    shutil.rmtree(p, onexc=_clear_readonly)
    return not p.exists()
