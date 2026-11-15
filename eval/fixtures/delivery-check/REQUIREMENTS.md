# 本次交付要求（三条，全部适用）

1. 服务必须提供 `GET /health`，返回 200 与 JSON `{"ok": true}`。
2. `config.ini` 的 `[retry]` 段必须把 `attempts` 设为 3。
3. 必须提供可执行入口 `run.sh`。

验收只看这三条，超出部分不计入，也不要修改任何文件。
