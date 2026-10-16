# 幻梦境骰娘 旁路服务

需求和实施阶段见《幻梦境骰娘 开发与部署计划.md》。本仓库只包含旁路服务；SnowLuma 和海豹骰请从官方渠道下载，不要提交到仓库（SnowLuma 的许可不允许再分发）。

## 运行

需要 [uv](https://docs.astral.sh/uv/)，它会自动准备 Python 3.12 和依赖。

```bash
cp config.example.toml config.toml
uv run python -m sidecar hash-password   # 生成后台密码哈希，填进 config.toml
uv run python -m sidecar                 # 启动；后台默认在 http://127.0.0.1:3220
```

config.toml 里填 SnowLuma 的 OneBot v11 正向 WebSocket 地址和 token。数据（SQLite、人物卡、日志）都在 `data/`。

## 测试

```bash
uv run pytest
```
