# LitePan-TGBot

本项目由 Codex + DeepSeek 创建并持续优化。

一个跑在独立容器里的 Telegram 机器人，用来远程操作 LitePan：发一条消息就能触发自动联动（刷新目录缓存、生成 STRM、刮削、Emby 刷库），跑完还会把结果推回来。它不改 LitePan 的任何源码，只调用 LitePan 自带的接口。

## 它是怎么工作的

机器人在 Telegram 那边收命令，然后调用你 LitePan 的接口：

- 触发自动化用的是开放接口 `POST /api/open/automation/events`（Bearer API Key）；
- 精确执行某条规则用的是管理接口 `/api/admin/automation/rules/{id}/run`（管理员登录会话）；
- 查看账号、任务、规则列表也走管理接口，所以它能自动知道你有几个盘、几条规则，不用手工配置；
- 完成回执是登录后台后轮询任务状态推回来的。

配好之后，你在 Telegram 里提到的“盘名”“规则名”都直接来自你的 LitePan，不需要在配置文件里再抄一遍。

## 部署

### 第一步：LitePan 后台准备

1. 系统设置 → API 秘钥，新建一个“任务执行”类型的 Key（以 `lpk_api_` 开头），复制保存；
2. 任务管理 → 自动联动，创建规则：触发方式选 Webhook，事件名随意（比如 `tg_refresh`），动作链里加上“刷新目录缓存 + STRM 任务”等动作；
3. （推荐）把 LitePan 的管理员账号密码也准备好，机器人才有权限自动读取规则、精确执行和推送完成回执。

### 第二步：装机器人

推荐用 Docker：

```bash
git clone https://github.com/MbAIGC/LitePan-TGBot.git
cd LitePan-TGBot
cp users.example.json users.json
# 编辑 users.json，填你自己的 chat_ids、litepan_url、api_key、admin_user、admin_password
docker compose up -d --build
```

也可以直接用构建好的镜像：

```bash
docker run -d --name litepan-tgbot --env-file .env -v ./data:/data \
  ghcr.io/MbAIGC/litepan-tgbot:latest
```

想先本地跑，装好 Python 3（记得 `pip install pypinyin`）后，`python3 tgbot.py --check` 检查配置，`python3 tgbot.py` 启动。

### 第三步：开始用

给机器人发 `/info`，它会告诉你 LitePan 连没连上、发现了哪些盘和规则、每个规则对应的菜单命令。

## 常用命令

- `/refresh`：触发所有规则，相当于全盘跑一遍。
- `/refresh <盘名>`：只触发这个盘下的规则，比如 `/refresh GY01`。
- `/refresh_<规则>`：命令菜单里的快捷命令，只触发这一条规则，比如 `/refresh_am_gy01_juji`。
- `/info`：查看连接状态、配置、发现的规则和盘名（`/list`、`/status`、`/ping` 也会走到这里）。
- `/menu`：手动重建命令菜单。
- `/run <事件>`：高级用法，手动触发任意 Webhook 事件；注意同名事件的所有规则会一起跑。

命令菜单会自动生成：每条规则对应一个 `/refresh_<规则>`，命令名是规则名的拼音缩写（中文没法直接进命令名，这是 Telegram 的限制），描述里显示后台规则全名。菜单每 30 分钟自动检查一次，有变化才更新；也可以随时发 `/menu` 手动刷新。

## 配置说明

配置主要放在 `users.json`（推荐）或 `.env`（单用户简化模式）：

- `chat_ids`：允许使用机器人的 Telegram 会话 ID；
- `litepan_url` / `api_key`：LitePan 地址和任务 Key；
- `admin_user` / `admin_password`：管理员账号，开启自动发现、精确执行和完成回执；
- `drives`：手动盘名映射（有管理员账号后一般不用配）；
- `default_event`：默认事件，没配管理员账号时 `/refresh` 的兜底事件；
- `show_url`：调试时是否在回复里显示 LitePan 地址，默认不显示。

## 几个常见问题

- 收不到完成回执？确认 `users.json` 里配了管理员账号，任务跑完才会推结果。
- 菜单没出现新规则？发一次 `/menu`，或等最多 30 分钟自动刷新；Telegram 客户端有时要重新打开输入框才显示。
- 为什么两条规则会一起触发？LitePan 的 Webhook 按事件名匹配，同名事件会全部触发，这是平台行为；单规则精确执行请用 `/refresh_<规则>`（走规则 ID）。
- 连不上 Telegram？大陆网络需要代理，或把 `TG_API_BASE` 指向 Bot API 镜像。

## 隐私

密码和 API Key 只在你自己的机器和 LitePan 之间传递，不会发给 Telegram；回复里默认也不显示 LitePan 地址。`users.json` 含敏感信息，记得 `chmod 600`，别提交到公开仓库。

## 说明

- 镜像：`ghcr.io/MbAIGC/litepan-tgbot`（GitHub Actions 自动构建，`latest` / `v*` / `sha-*` 标签）；
- 完整变更记录见 CHANGELOG.md；
- 本地测试：`python3 test_tgbot.py`。
