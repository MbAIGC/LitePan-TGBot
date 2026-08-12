# LitePan-TGBot 🎬

> 本项目由 **Codex + DeepSeek** 创建并持续优化。

一个 Telegram 遥控器，用来远程操作 **LitePan**：在 TG 里发一条消息，就能触发 LitePan 的自动联动（刷新目录缓存、生成 STRM、刮削、Emby 刷库），跑完还会把结果推回来。全程不改 LitePan 源码，独立容器部署，换机器、换账号都只需要改一份配置。

![CI](https://github.com/MbAIGC/LitePan-TGBot/actions/workflows/docker-image.yml/badge.svg)

## ✨ 功能一览

- **远程触发**：`/refresh` 一键跑所有规则，也可以按盘、按单条规则精确触发；
- **自动发现**：登录 LitePan 后自动读取账号、任务、规则，盘名和规则名不用手工配置；
- **精确执行**：单规则命令走规则 ID，同名通知事件也不会误触发；
- **完成回执**：任务跑完自动推送 `✅/❌` 和步骤明细；
- **命令菜单**：每条规则自动生成一个 TG 菜单命令，新增规则自动更新；
- **隐私友好**：地址默认不显示，密码和 Key 不经过 Telegram。

## 🔧 工作原理

```text
Telegram 消息
  → Bot（独立进程，长轮询）
  → 触发规则：POST /api/open/automation/events（API Key）
  → 精确执行：POST /api/admin/automation/rules/{id}/run（管理员会话）
  → 完成回执：轮询 /api/admin/automation/runs
```

自动化规则、STRM 任务、刮削策略全部沿用你 LitePan 后台已有的配置，Bot 只负责“按一下按钮”。

## 🚀 Docker 部署

### 1️⃣ LitePan 后台准备（一次性）

1. 系统设置 → **API 秘钥**，新建一个“任务执行”类型 Key（`lpk_api_` 开头），复制保存；
2. 任务管理 → **自动联动**，创建规则：触发方式选 **Webhook**，事件名随意（如 `tg_refresh`），动作链加“刷新目录缓存 + STRM 任务”等动作；
3. （推荐）准备 LitePan 管理员账号密码：用于自动发现、精确执行和完成回执。

> 注意：LitePan 平台要求“刷新目录缓存”后面必须有整理任务或 STRM 任务，规则才能执行成功。

### 2️⃣ 启动 Bot

```bash
git clone https://github.com/MbAIGC/LitePan-TGBot.git
cd LitePan-TGBot
cp users.example.json users.json
```

编辑 `users.json`，填上你自己的信息：

```json
{
  "users": [
    {
      "chat_ids": [123456789],
      "litepan_url": "http://你的NAS:5211",
      "api_key": "lpk_api_xxxx",
      "default_event": "tg_refresh",
      "drives": { "GY01": "tg_am_gy01_juji" },
      "admin_user": "admin",
      "admin_password": "你的密码",
      "show_url": false
    }
  ]
}
```

然后启动：

```bash
docker compose up -d --build
docker compose logs -f
```

不想自己构建，直接用 GitHub Actions 出品的镜像：

```bash
docker run -d --name litepan-tgbot \
  --env-file .env \
  -v ./data:/data \
  ghcr.io/MbAIGC/litepan-tgbot:latest
```

### 3️⃣ 开始使用

给机器人发 `/info`：它会告诉你 LitePan 连没连上、发现了哪些盘和规则、每条规则对应的菜单命令。

## 📖 命令说明

| 命令 | 作用 |
| --- | --- |
| `/refresh` | 触发所有规则（相当于全盘跑一遍） |
| `/refresh <盘名>` | 只触发这个盘下的规则，如 `/refresh GY01` |
| `/refresh_<规则>` | 菜单快捷命令，精确触发单条规则，如 `/refresh_am_gy01_juji` |
| `/info` | 查看连接状态、配置、规则与盘名（`/list` `/status` `/ping` 同效） |
| `/menu` | 手动重建命令菜单 |
| `/run <事件>` | 高级：手动触发任意 Webhook 事件，同名事件会全部触发 |

菜单里每条规则对应一个 `/refresh_<规则>`：命令名是规则名的拼音缩写（Telegram 命令名不允许中文），描述显示后台规则全名。菜单每 30 分钟自动检查一次，有变化才更新，也可以随时发 `/menu`。

## ⚙️ 配置项

| 配置 | 位置 | 说明 |
| --- | --- | --- |
| `chat_ids` | users.json | 允许使用 Bot 的 Telegram 会话 ID |
| `litepan_url` / `api_key` | users.json / .env | LitePan 地址和任务 Key |
| `admin_user` / `admin_password` | users.json / .env | 管理员账号，开启自动发现、精确执行和回执 |
| `drives` | users.json / .env | 手动盘名映射（有管理员账号后一般不用配） |
| `default_event` | users.json / .env | 兜底事件（env 里为 `LITEPAN_FALLBACK_EVENT`，旧名 `LITEPAN_EVENT` 兼容），未配管理员时 `/refresh` 使用 |
| `show_url` | users.json / .env | 调试时是否显示 LitePan 地址，默认不显示 |
| `TG_MENU_REFRESH_MINUTES` | .env | 菜单自动刷新间隔，默认 30 分钟 |

## ❓ 常见问题

- **`/refresh` 触发所有规则，后台需要设置联动吗？**
  需要。`/refresh` 只是把 LitePan 后台已有的 Webhook 自动化规则全部跑一遍。后台一条规则都没建时，它会提示“没有发现规则”；规则建好后无需在 Bot 侧做任何配置，自动发现会直接读到。

- **收不到完成回执？**
  确认 `users.json` 里配了管理员账号，任务跑完才会推结果。

- **旧的 `.env` 还能用吗？**
  兼容。新增的 `LITEPAN_FALLBACK_EVENT` 会自动回退读取旧名 `LITEPAN_EVENT`；唯一例外是旧 `.env`
  如果没配 `TG_ALLOWED_IDS`，现在启动会报错要求补上——这是安全修复，防止任何陌生人使用你的 Bot。

- **菜单没出现新规则？**
  发一次 `/menu`，或等最多 30 分钟自动刷新；Telegram 客户端有时要重新打开输入框才显示。

- **为什么两条规则会一起触发？**
  LitePan 的 Webhook 按事件名匹配，同名事件会全部触发，这是平台行为；单规则精确执行请用 `/refresh_<规则>`（走规则 ID）。

- **连不上 Telegram？**
  大陆网络需要代理，或把 `TG_API_BASE` 指向 Bot API 镜像。

## 🔒 隐私

密码和 API Key 只在你自己的机器与 LitePan 之间传递，不会发给 Telegram；回复里默认不显示 LitePan 地址。`users.json` 含敏感信息，记得 `chmod 600`，别提交到公开仓库。

## 📜 说明

- 镜像：`ghcr.io/MbAIGC/litepan-tgbot`（CI 自动构建，`latest` / `v*` / `sha-*` 标签）；
- 变更记录见 [CHANGELOG.md](CHANGELOG.md)；
- 本地测试：`python3 test_tgbot.py`。
