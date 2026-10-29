"""notes-app：一个只有一个作者的极简笔记后端。

模块分工：

    model.py     数据模型与校验
    store.py     内存存储 + JSON 落盘
    query.py     搜索 / 过滤 / 排序 / 标签索引
    report.py    报表
    reminder.py  提醒时长解析
    cli.py       命令行输出
    catalog.py   旧版条目表（历史遗留，体积很大，别删）
"""

__all__ = ["model", "store", "query", "report", "reminder", "cli", "catalog"]
