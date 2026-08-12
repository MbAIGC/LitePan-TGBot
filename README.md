# LitePan-TGBot —— LitePan 零改动 Telegram Bot

## 结论

**可以，完全不需要改动 LitePan 源码。** LitePan（Go 版）本身提供了开放 Webhook 触发入口和 API Key 鉴权，Telegram Bot 作为独立进程给这套机制加一个“遥控器”即可。

```
Telegram 消息
  -> 本 Bot（长轮询 getUpdates，独立进程）
  -> POST /api/open/automation/events（Bearer API Key）
  -> LitePan 自动化规则（Webhook 触发）
  -> 动作链：刷新缓存 / STRM 生成 / 刮削 / Emby 刷库（异步执行）
```

本方案依赖的 LitePan 现有接口（已在源码中核实）：

| 用途 | 接口 | 鉴权 |
| --- | --- | --- |
| 触发自动化 | `POST /api/open/automation/events` | Bearer API Key（`task` 类型） |
| 连通性自检 | `GET /api/health` | 无 |
| 回执轮询（可选） | `GET /api/admin/automation/runs?rule_id=` | 管理员登录会话 |

LitePan 的 webhook 解析器只读取 `event`、`source`、`path`（`delayTime` 兼容 CloudSaver），
请求体里多余的字段（如 `message`）会被忽略但不报错。API Key 以 `lpk_api_` 开头。

## 一、LitePan 后台配置（一次）

1. **创建 API Key**：系统设置 → “API 秘钥” → 新建，类型选 **任务执行（task）**。密钥只显示一次，立即复制。
2. **创建自动化规则**：任务管理 → 自动联动 → 新增：
   - 触发方式：**Webhook**
   - 触发事件：填一个自定义名称，如 `tg_refresh`（与 `.env` 里 `LITEPAN_EVENT` 一致；`download_completed` 这类任意事件名同理可用）
   - 动作链：按需编排，例如 `刷新目录缓存 → STRM 生成 → STRM 刮削 → Emby 刷库`（可加 `延时`）
3. **先手动验证链路**（把 `<KEY>`、`<地址>` 换成你的）：

   ```bash
   curl -X POST http://127.0.0.1:5211/api/open/automation/events \
     -H "Authorization: Bearer <你的API Key>" \
     -H "Content-Type: application/json" \
     -d '{"event":"tg_refresh","source":"telegram","path":"/","message":"test"}'
   ```

   返回 `matched: 1` 即表示规则命中，动作链已开始异步执行。
   注意：`message` 字段仅供留痕，规则匹配只看 `event/source/path`。

## 二、部署 Bot

### 方式 1：Docker Compose（推荐）

```bash
cd LitePan-TGBot   # 项目根目录
cp .env.example .env
# 编辑 .env，填写 TG_BOT_TOKEN、TG_ALLOWED_IDS
# 然后二选一配置 LitePan 连接：
#   A. 复制 users.example.json 为 users.json，填自己的地址/密钥/盘名映射（推荐）
#   B. 直接在 .env 填 LITEPAN_URL / LITEPAN_API_KEY，可选 DRIVES 盘名映射
docker compose up -d --build
docker compose logs -f
```

也可以直接用 GitHub Actions 构建好的镜像（见下文）：

```bash
docker run -d --name litepan-tgbot \
  --env-file .env \
  -v ./data:/data \
  ghcr.io/<你的GitHub用户名>/litepan-tgbot:latest
```

### 方式 2：直接运行（仅需 Python 3，无第三方依赖）

```bash
cd litepan-tgbot
set -a && source .env && set +a   # 或 export 各环境变量
python3 tgbot.py --check          # 先检查配置
python3 tgbot.py                  # 启动
```

## 三、可用命令

| 命令 | 说明 |
| --- | --- |
| `/refresh` | 触发本会话的默认规则（即“全盘”规则，动作链里挂了几个盘就刷几个） |
| `/refresh <盘名>` | 触发指定盘对应的规则，如 `/refresh 光鸭-A`（盘名取自 LitePan 账号，无需手工配置） |
| `/refresh /路径` | 触发默认规则，路径仅作记录（LitePan 规则匹配不依赖路径） |
| `/list` | 自动列出 LitePan 里的账号、规则（事件→名称→涉及任务）与盘名 |
| `/run <事件> [路径]` | 触发任意 Webhook 事件，如 `/run quarkA_refresh` |
| `/ping` | 检查本会话 LitePan 连通性 |
| `/status` | 查看本会话绑定配置与连通性 |
| `/start` `/help` | 帮助 |

## 三·五、不用手工配盘名：自动发现

之前版本要你在配置里把“盘名 → 事件名”抄一遍，容易和 LitePan 后台对不上。现在只要在配置里填上**管理员账号**，Bot 会自动登录 LitePan，读取账号列表、STRM 任务列表和自动化规则列表，**自动生成映射**：

- `/refresh 光鸭-A` → 直接按 LitePan 里的**账号名**匹配，触发涉及该账号任务的规则；
- `/list` → 直接显示 LitePan 里的规则：`事件 → 规则名 → 涉及任务`，如 `quarkA_refresh → 「光鸭-A刷新」，任务：光鸭-A-TV`；
- `default_event` 填 `auto`（或留空）时，`/refresh` 自动采用唯一一条 Webhook 规则；有多条时 `/list` 会提示你指定。

管理员账号本来就是为了“完成回执”而配的，现在一份配置同时解决回执和盘名自动发现，无需再维护 `DRIVES`。`DRIVES` 降级为“手动覆盖”：配了就优先用它，适合不想给 Bot 管理员权限的用户。

## 三·六、不同用户怎么用（每用户独立部署）

场景是**每个用户私有部署自己的 LitePan + 自己的 Bot**，代码里**没有任何硬编码的盘名或事件名**，盘名直接来自各自 LitePan 的账号列表。

例如：

| | 用户 A | 用户 B |
| --- | --- | --- |
| LitePan | litepan-A（自己 NAS） | litepan-B（自己 NAS） |
| Bot | bot-A | bot-B |
| 网盘账号 | 光鸭-A、123-A | 115-B、天翼-B |
| STRM 任务 | 光鸭-A-TV、123-A-DSJ | 115-B-ABC、天翼-B-123 |
| 各自配置 | users.json 单条目 | users.json 单条目 |

每个用户复制同一套 Bot 代码，在**自己的部署环境**里填自己的配置（`users.json` 或 `.env`），例如用户 A：

```json
{
  "users": [
    {
      "chat_ids": [123456789],
      "litepan_url": "http://A-NAS:5211",
      "api_key": "lpk_api_xxxA",
      "default_event": "tg_refresh",
      "drives": {
        "光鸭-A": "quarkA_refresh",
        "123-A": "123A_refresh"
      }
    }
  ]
}
```

用户 B 完全一样，只是把地址换成 `http://B-NAS:5211`、密钥换成自己的、`drives` 填：

```json
"drives": {
  "115-B": "115B_refresh",
  "天翼-B": "tianyiB_refresh"
}
```

- `drives` 里的“盘名”是用户自己起的，值是该用户 LitePan 后台里建的 Webhook 事件名；
- `/refresh` → 自己的默认规则（全盘）；`/refresh 光鸭-A` → 自己的 `quarkA_refresh`；`/list` 随时查看；
- 完成回执里显示的规则名就是 LitePan 里的名字（如 `光鸭-A-TV`），无需额外配置；
- 不加 `chat_ids` 时，Bot 只接受 `TG_ALLOWED_IDS` 里的会话；建议两者都配成自己的 chat_id。

配置了管理员账号后，上面这些盘名映射都可以不填，`/list` 自动列出，`/refresh 光鸭-A` 直接用账号名。

### 隐私设置

回复默认**不显示 LitePan 地址**，只显示云盘名、规则名、任务名和状态（这些是你自己在 LitePan 里起的名字）。
需要调试时再开启地址显示：users.json 里 `"show_url": true`，或 .env 里 `SHOW_LITEPAN_URL=1`。
密码和 API Key 在任何情况下都不会出现在回复或日志中。

> 说明：`users.json` 支持多条记录，但按当前模型每个 Bot 只服务一个用户，通常只填一条即可。

## 四、回执模式（可选）

默认只推送“已触发”，因为任务执行是异步的，而 LitePan 的开放接口不提供任务状态查询。

如果你希望 Bot 推送**完成/失败回执**，在 `.env` 里补上 LitePan 管理员账号：

```bash
LITEPAN_ADMIN_USER=admin
LITEPAN_ADMIN_PASSWORD=你的管理员密码
```

Bot 触发后会用该账号登录（沿用现有 `/api/auth/login` 与 `/api/admin/automation/runs` 接口，仍为零源码改动），轮询对应规则的运行状态，结束后推送 `✅/❌` 与步骤结果。

> 安全提示：密码会明文存于环境变量，权限等同 LitePan 管理员，请仅在信任的部署环境使用；更彻底的方案是把回执接入 LitePan 事件总线（需改源码，见下）。

## 五、已知局限与注意事项

- **完成回执的两种取法**：上面用管理员会话轮询（零改动）；若想实时推送，需在 LitePan 内置 tgbot 服务并订阅事件总线（改源码）。
- **自动发现依赖管理员账号**：未配置时 `/refresh <盘名>` 只能用手工 `DRIVES` 映射；配置后 `/list`、盘名匹配、完成回执全部可用。管理员会话默认 2 小时，Bot 在 401 时会自动重新登录。
- **单实例**：Telegram `getUpdates` 长轮询只能单实例运行，多个副本会互相抢消息（409）。
- **网络**：Bot 需要能访问 `api.telegram.org`。大陆网络可用代理或 Bot API 镜像，通过 `TG_API_BASE` 指定；`LITEPAN_URL` 只要 Bot 能访问到 LitePan 即可，不必公网。
- **`cache_clear` 语义**：自动化里的“刷新目录缓存”不是全盘清空，而是刷新后续整理/STRM 动作涉及的目录缓存，规则中需包含后续动作；全量清缓存走管理页接口。
- **权限**：建议务必配置 `TG_ALLOWED_IDS` 白名单；API Key 泄露即可触发规则，注意保管。
- **状态文件**：`TG_STATE_FILE`（默认 `tgbot-state.json`，容器内为 `/data/state.json`）用于记住更新游标，重启不重复触发。

## 六、多盘联动建议（配合自动化规则）

在自己的 LitePan 后台（以用户 A 为例）：

1. 每个网盘账号建独立的 STRM 任务：账号=光鸭-A → 任务 `光鸭-A-TV`；账号=123-A → 任务 `123-A-DSJ`；
2. 建“全盘”规则：Webhook 事件名 `tg_refresh`，动作链挂上全部 STRM 任务（光鸭-A-TV、123-A-DSJ）；
3. 每个盘再建单盘规则：事件名如 `quarkA_refresh`（只挂光鸭-A-TV）、`123A_refresh`（只挂123-A-DSJ）；
4. 配置里 `default_event=tg_refresh`，`drives` 填盘名映射，TG 端即可：
   `/refresh` 全盘、`/refresh 光鸭-A` 只刷光鸭-A、`/list` 随时查看。

事件名、盘名、任务名都可以按自己的习惯起，只要 Bot 配置与 LitePan 后台规则一致即可。

## 七、CI / 镜像构建

仓库内置 GitHub Actions 工作流 [.github/workflows/docker-image.yml](.github/workflows/docker-image.yml)：

- 推送到 `main` 分支、打 `v*` 标签，或手动触发 `workflow_dispatch` 时，自动构建并推送到 **GitHub Container Registry（GHCR）**；
- 镜像名：`ghcr.io/<你的GitHub用户名>/litepan-tgbot`；
- 标签规则：`latest`（默认分支）、`v*`（版本标签）、`sha-<commit>`（每次构建）；
- 推送使用仓库自带的 `GITHUB_TOKEN`，无需额外配置 Secret。

部署时把 `docker-compose.yml` 里的 `build: .` 换成 `image: ghcr.io/<你的GitHub用户名>/litepan-tgbot:latest` 即可直接用镜像。
