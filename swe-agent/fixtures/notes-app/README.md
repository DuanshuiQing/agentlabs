# notes-app

一个只有一个作者的极简笔记后端。没有数据库、没有 Web 层，只有 Python 模块和一个
`unittest` 套件。生产环境的入口在 `notes/cli.py`，数据用 `notes/store.py` 落成 JSON。

## 跑测试

```
python -m unittest discover -s tests -t . -v
```

## 模块

| 模块 | 职责 |
| --- | --- |
| `notes/model.py` | `Note` 数据模型与校验 |
| `notes/store.py` | 内存存储 + JSON 读写 |
| `notes/query.py` | 搜索 / 过滤 / 排序 / 标签索引 |
| `notes/report.py` | 标签排行、摘要、平均长度 |
| `notes/reminder.py` | 提醒时长解析（`1h30m`） |
| `notes/cli.py` | 命令行渲染与摘要 |
| `notes/catalog.py` | 旧版条目表（400 条硬编码，别删） |

## 约定

- **测试是契约**：`tests/` 里的断言规定了行为，改实现去满足它，不要为了方便改断言。
- 已知问题与维护备忘见 [docs/TODO.md](docs/TODO.md)。
