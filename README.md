# 幻梦境骰娘 旁路服务

需求和实施阶段见《幻梦境骰娘 开发与部署计划.md》。本仓库只包含旁路服务；SnowLuma 和海豹骰请从官方渠道下载，不要提交到仓库（SnowLuma 的许可不允许再分发）。

## 在 Windows 10 上部署（骰娘号挂在现有的 SnowLuma 上）

整体结构：SnowLuma 登录骰娘的 QQ 号，开一个 OneBot v11 WebSocket 服务端；海豹骰和本服务都作为客户端连上去。海豹负责掷骰，本服务负责记团、复读链、轮盘、入群欢迎、拉群校验、人物卡和网页后台。

### 1. SnowLuma：加上骰娘号

1. 在 SnowLuma 后台给骰娘号扫码登录。
2. 在这个账号的网络配置里加一个 WebSocket 服务端，设置一个不和现有服务冲突的端口，并填一个随机 token，记下来。

### 2. 海豹骰

1. 安装 Windows 版海豹，在它的后台添加账号，类型选 OneBot v11 正向 WebSocket，填上一步的地址和 token。
2. 不要安装记团、俄罗斯轮盘相关的 JS 插件，这些都由本服务处理。
3. 在大群里发 `.bot off`，关掉海豹在大群的掷骰（大群规定骰娘在大群不开放）。

### 3. 本服务

先安装 [uv](https://docs.astral.sh/uv/)（它会自动准备 Python 3.12 和依赖），在 PowerShell 里运行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

然后下载本仓库并准备配置：

```powershell
git clone https://github.com/Aozaki2501/huanmeng-dice-sidecar.git
cd huanmeng-dice-sidecar
copy config.example.toml config.toml
uv run python -m sidecar hash-password
```

用记事本打开 `config.toml`：

- `[onebot]`：填第 1 步的 WebSocket 地址（如 `ws://127.0.0.1:端口`）和 token。
- `[web] admin_password_hash`：填上一条命令输出的那一长串。
- `[backup] dir`：填网盘同步文件夹里的一个目录，用于每周备份；`sealdice_backups` 填海豹安装目录下的 `backups`。

试运行：双击 `scripts\start.bat`。看到「已连接」和「骰娘账号：…」就说明连上了。浏览器打开 http://127.0.0.1:3220 登录后台。

### 4. 后台里要设置的

- **配置**页：「大群群号」必填；复读检测默认只检测大群。
- **团账本**页：「导入名单」把群里现行的记团名单整段粘贴进来，再逐个填 QQ 号。
- **文案**页：所有骰娘说的话都能改，保存后立即生效。

### 5. 开机自启

```powershell
powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1
```

它会在「启动」文件夹放一个快捷方式：登录 Windows 后等 60 秒（让 SnowLuma 和 QQ 先起来），再最小化运行 `scripts\start.bat`。服务意外退出会在 10 秒后自动重启。取消自启：同一条命令后面加 ` -Remove`。

因为是登录后才启动，这台电脑需要设置开机自动登录 Windows。

## 日常维护

- **掉线提醒**：骰娘连不上 SnowLuma、QQ 掉线或 SnowLuma 卡住超过 5 分钟，电脑右下角会弹通知，后台每页顶部也会显示红字。QQ 大约每 30 天要重新扫码一次。
- **备份**：每周自动一次。想立即备份：`uv run python -m sidecar backup`。
- **恢复**：先关掉 start.bat 的窗口，再运行 `uv run python -m sidecar restore 备份目录\db\sidecar-日期.db`，输入 yes 确认。原来的数据库会改名留底，不会直接覆盖丢失。
- **日志**：`data\logs\`，按天保留 14 天。
- **更新本服务**：关掉 start.bat 的窗口，在仓库目录运行 `git pull`，再双击 `scripts\start.bat`。

## 高加索轮盘（五颗子弹）

普通 `.轮盘` 仍是一颗子弹，中弹一人记一团后结束。高加索轮盘是六个弹仓装五颗子弹，随机留一个空弹位；六人各开一枪，五位中弹者各记一团，唯一空枪者免记团，打完六枪本局结束。

```text
.高加索轮盘 报名    满六人开局
.高加索轮盘 退出    开局前取消报名
.高加索轮盘 掷点    每人掷 d100，大者先开枪，同点重掷
.高加索轮盘         查看当前状态
.开枪               轮到自己时开枪
.高加索轮盘 结束    群管理或记团管理员强制结束
```

`.高加索` 是同义指令，也支持 `join / quit / roll / shoot / off / help`。每群同时只能有一局普通或高加索轮盘。每次中弹立即记团并公告；中途强制结束或超时，已记的团保留，未开枪的人不再记团。超时时长沿用后台的「轮盘超时分钟数」。高加索轮盘的专属文案可以在后台「文案」页修改。

## 开发

```bash
uv run python -m sidecar   # 启动；后台默认在 http://127.0.0.1:3220
uv run pytest              # 测试
```

数据（SQLite、人物卡、日志）都在 `data/`。
