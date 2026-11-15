# ledger-service

一个记录收支的小服务。启动入口、默认端口和路由表都在 `service/` 里，本文件不重复这些常量，
避免两处失配。

- `service/main.py`：命令行入口
- `service/routes.py`：对外路由
- `service/store.py`：内存账本
- `tests/`：轻量回归
