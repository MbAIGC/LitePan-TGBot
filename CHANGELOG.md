# CHANGELOG

本项目由 Codex + DeepSeek 创建并持续优化。下面按时间顺序记录主要变化，方便回溯每一版做了什么、为什么这么做。

## v0.13（未发布）：三模型 review 待优化清单（2026-08-13）

> 状态：待优化 → 已于 v0.14 处理（2026-08-13）。以下清单由 GPT-5.6、DeepSeek-V4-Pro、DeepSeek-V4-Flash 三个模型对 `tgbot.py`、`test_tgbot.py`、`Dockerfile`、CI 配置及文档的 review 汇总而成；除「多用户菜单隔离」按部署模型确认不修、HTTP mock 测试（401/409/超时）尚待补外，其余条目已在 v0.14 修复。

### P0 安全网：测试与 CI

- [ ] 待优化：新增 `requirements.txt`（固定 `pypinyin==0.54.0`），README 补充本地测试的依赖安装说明；CI 增加 `python3 -m unittest`/pytest 测试步骤。当前 `test_tgbot.py` 在干净环境首个断言即失败（`光鸭` 被转成 `pan1` 而非 `guangya`），且 `.github/workflows/docker-image.yml` 只构建推送不跑测试，坏测试可进入 main。
- [ ] 待优化：增加真实 HTTP mock 测试，覆盖 401 重登录、409 冲突、超时、运行状态变化等场景。

### P1 配置解析健壮性

- [ ] 待优化：统一封装 `ConfigError`，替换裸 `int()`/`bool()` 解析（`TG_ALLOWED_IDS=abc`、`users.json` 中 `"lite_timeout": "abc"` 目前会直接 traceback 崩溃）。
- [ ] 待优化：新增 `parse_bool()`，修复 `"show_url": "false"` 被 `bool()` 解析为 `True`、导致 LitePan 地址被意外显示的问题。
- [ ] 待优化：配置边界校验——`lite_timeout >= 1`、`receipt_poll >= 1`（0 会造成忙轮询，负数让 `sleep()` 抛异常）、`receipt_timeout >= receipt_poll`、`chat_ids` 必须为整数且不可重复（重复目前静默覆盖前一个用户配置）。

### P1 消息处理可靠性（游标 / 幂等 / 回执）

- [ ] 待优化：更新轮询在处理消息前就推进 offset，且异常后仍保存游标，存在“确认但未执行”的消息丢失风险；调用 LitePan 成功后崩溃又可能重复触发。建议明确 at-least-once 投递、为命令增加幂等键，并把处理结果与游标持久化设计清楚。
- [ ] 待优化：`_save_offset` 非原子写，进程中途被杀可能损坏游标文件，重启后 offset 回退导致 Telegram 重推旧消息；`by_event`、`account_slug` 两个字段只写不读，属死代码。
- [ ] 待优化：`watch_run` 回执取“比快照更大的最小运行 ID”，同一规则短时间内触发两次时会死等第一个运行结束；若第一个运行卡住，第二个早已完成也收不到回执（建议按规则追踪最新运行 ID）。

### P2 自动发现与 slug 命名

- [ ] 待优化：自动发现字段 `int(... or 0)` 解析脆弱，远端 API 脏字段抛异常且被兜底逻辑吞掉，`/info`、`/refresh` 静默失败；应集中容错、降级为“自动发现失败”并输出可定位日志。
- [ ] 待优化：账号 slug 与规则 slug 未共用命名空间，规则优先匹配，`/refresh_<slug>` 永远命中规则，账号级“触发该盘全部单盘规则”入口不可达；建议统一维护 used 集合或采用可区分命名。
- [ ] 待优化：规则归属判定过宽，仅凭一个已知任务属于某账号就把整条规则归入；应记录所有动作的解析结果，只有全部相关任务均成功解析且属于同一账号时才归入单盘规则。

### P2 运行状态判断

- [ ] 待优化：当前将所有 `!= "running"` 的状态都视为终态，LitePan 处于 queued/pending/waiting 时会提前发送“完成”回执；应显式定义终态（success/failed/error/cancelled），其余状态继续轮询。

### P3 多用户菜单隔离（确认不修）

- [x] 确认不修：部署模型为一个 LitePan 实例对接一个 LitePan-TGBot，使用人通过 `TG_ALLOWED_IDS` 白名单控制，所有使用人共享同一实例的同一份规则，全局 `setMyCommands` 保持菜单一致是正确行为；多用户菜单隔离不适用，无需按 chat_id 设置 scope。

### P3 文档与打磨

- [ ] 待优化：`/info` 将自动发现结果为 `None` 一律显示“自动发现未开启（需管理员账号）”，实际可能只是请求失败，需区分文案。
- [ ] 待优化：`users.json` 存在但为空时不回退 `.env` 单用户模式——复核确认行为合理（文件存在即表示用户选择了 users.json 模式，报错比静默回退更安全），仅优化报错文案，不动逻辑。
- [ ] 待优化：`TG_ALLOWED_IDS` 与 `users.json` 并存时 env 完全覆盖 users.json——复核确认为白名单安全特性（更严格更安全），不改代码，README 需文档化该行为。
- [ ] 待优化：Docker 以 root 运行、基础镜像未锁 digest、无健康检查/只读文件系统；CI action 仅锁 tag 未锁 SHA，存在供应链风险。

## v0.14：逐项修复三模型 review 问题（2026-08-13）

针对 v0.13 待优化清单逐项修复（多用户菜单隔离按部署模型确认不修）：

- 测试与 CI：新增 `requirements.txt`（固定 `pypinyin==0.54.0`）；CI 增加 test job，安装依赖后运行 `test_tgbot.py` 与 `test_robustness.py`，构建依赖测试通过；仅改 .md 文档（CHANGELOG / 优化记录 / README）时跳过测试与构建；
- 配置解析：新增 `_parse_bool`/`_parse_int`，users.json 的 show_url / lite_timeout / receipt_poll / receipt_timeout 严格校验（`receipt_poll=0`、`lite_timeout=0`、`receipt_timeout < receipt_poll`、show_url 非法值均报 `ConfigError`）；chat_ids 重复直接报错；`TG_ALLOWED_IDS` 非法值报 `ConfigError` 而不是 traceback；`SHOW_LITEPAN_URL` 同样走严格布尔解析；
- 消息可靠性：getUpdates 改为“处理成功才推进 offset 并原子落盘”（临时文件 + `os.replace`），单条消息失败重试 5 次后跳过，避免坏消息卡死轮询；`watch_run` 按 `(rule_id, run_id)` 去重回执，同一规则多次触发各自回执，不再死等最早运行；显式终态 `success/failed/error/cancelled`，queued/pending/waiting 继续轮询；
- 自动发现：字段解析集中容错（`_int_field`/`_str_field`），整体异常降级为“自动发现失败”并记录日志；删除只写不读的 `by_event`、`account_slug`；账号 slug 与规则 slug 共用命名空间（冲突时账号自动加 `_2` 后缀）；规则归属收紧为“所有动作解析成功且全部属于同一账号”，未知任务/其他动作不再误归入单盘；
- 文案与文档：`/info` 区分“自动发现未开启”与“自动发现失败”；users.json 存在但为空时报明确错误（不静默回退 .env，行为不变）；README 与 .env.example 文档化 `TG_ALLOWED_IDS` 优先覆盖 users.json 的白名单行为；
- Docker：基础镜像锁 digest，改为非 root 用户 `app` 运行，新增 `--health` 健康检查；docker-compose 增加 `read_only` 与 `tmpfs /tmp`；
- 新增 `test_robustness.py` 覆盖以上修复（配置校验、游标 at-least-once、回执去重、slug 冲突、规则归属、终态、/info 文案），原有测试全部通过。

## v0.15：/refresh 支持“全量规则 all”约定（2026-08-13）

- 约定：后台存在规则名（不区分大小写）为 `all` 的 Webhook 规则时，它被视为“全量规则”，对应菜单命令 `/refresh_all`；
- `/refresh`（不带参数）检测到全量规则时只触发它，不再逐个触发其他规则，避免 `all` 与单盘/单任务规则重复执行；没有该规则时保持原行为（触发所有规则）；
- 其他规则仍可用 `/refresh <盘名>`、`/refresh_<规则>` 精确触发；
- 适用场景：不是每个 STRM 任务都建了联动规则时，用一条 `all` 规则兜底做全量刷新（刷新目录 + 生成所有 STRM 任务）；
- README 增加 `> [!WARNING]` 说明该约定，测试补充“全量规则只触发 all”用例。
- Dockerfile 基础镜像取消 digest 锁定，改回 `python:3.12-alpine`（跟随 tag 更新，不再字节级锁定）。

## v0.12：全量 review 整改（2026-08-12）

针对整体 review 列出的问题逐项修复：

- 菜单命令 slug 限长 24 字符（保证 `refresh_` 前缀后不超过 Telegram 的 32 字符上限），并保持重名去重；菜单命令总数超过 100 条时截断并告警；
- 单用户 .env 模式强制要求 `TG_ALLOWED_IDS` 白名单，未配置启动即报错，防止机器人对所有人开放；
- 回执快照失败不再阻断规则执行（单规则路径与多规则路径行为一致）；
- `/info` 超长时按行分块发送，避免超过 Telegram 单条消息 4096 字符上限；
- 双实例 409 冲突改为长退避重试而不是直接退出，避免容器反复重启空转；
- 多条规则并发触发时，回执轮询复用已登录的客户端，管理员登录用全局锁串行化，避免并发登录风暴；
- Dockerfile 固定 pypinyin 版本，构建可复现；
- `users.json` 的 chat_ids 支持逗号/中文逗号/空白分隔的字符串，非法值给出明确报错；
- `/refresh` 在后台没有规则时直接提示“还没有规则”，不再绕一圈报“未匹配”；
- 启动时菜单刷新改为后台线程，LitePan 不可达不再拖慢启动；
- 增加 SIGTERM/SIGINT 优雅退出，退出前保存更新游标；
- `/run` 帮助文案去掉无效的路径参数；
- 新增测试：slug 限长、超长消息分块、快照失败不阻断执行、单用户模式白名单校验。

兼容性：`LITEPAN_EVENT` 更名为 `LITEPAN_FALLBACK_EVENT`，旧名仍可读；其余配置项不变。

## v0.11：修复完成回执丢失（2026-08-12）

`/refresh` 触发多条规则时，先执行的规则可能收不到回执。原因是回执轮询在“触发之后”才快照已有运行 ID：规则跑得太快时，运行记录在快照前就已建好（甚至已完成），轮询里“ID 大于快照”的判断永远不成立，回执被跳过。修复方式：触发前先快照全局最大运行 ID，回执只认快照之后产生的新运行，无论规则多快都能识别并推送。

## v0.10：界面与文档美化（2026-08-12）

根据实际使用反馈优化展示和阅读体验：

- `/info` 重新排版：分区块展示（状态、配置、规则、盘名、提示），规则按“名称 + 事件 + 命令 + 任务”分行列出，去掉靠空格对齐的写法（Telegram 里非等宽字体下对齐会乱）；
- README 重写为带目录感的结构化文档：功能一览、工作原理、Docker 部署分步说明、命令表、配置表、常见问题，并加了 CI 徽章；
- 常见问题里补充说明“`/refresh` 触发所有规则仍需后台建好 Webhook 自动化规则”。

## v0.9：命令体系整改（2026-08-12）

这一轮主要根据实际使用反馈简化命令、减少噪音：

- `/list`、`/status`、`/ping` 合并成 `/info`，一条消息看完连接状态、配置、规则和盘名；旧命令保留为别名，不影响习惯。
- `/refresh` 改成“触发所有规则”，语义和文案都改清楚；去掉了 `/refresh /路径` 这种写法，因为路径本来就不参与动作链。
- `/refresh <盘名>` 和 `/refresh_<规则>` 改为按规则 ID 精确执行，解决“同名通知事件全部触发”的问题：这是 LitePan Webhook 的平台行为，单规则触发走管理接口 `/api/admin/automation/rules/{id}/run`。
- `/run` 降级为高级命令，移到命令菜单最末，避免误用。
- 新增 `/menu` 手动刷新菜单；菜单默认 30 分钟检查一次，只有命令列表真的变了才调用 Telegram 接口，降低无谓请求。
- `/info` 里删掉容易误导的“规则别名”行，只有配置了手动映射时才显示，并改名“手动映射(DRIVES)”。
- 文档改写为自然语言版本，原“优化总结.md”更名为本文件（CHANGELOG.md）。

## v0.8：按规则 ID 精确执行（2026-08-12）

发现两条规则共用同一个通知名称（如 `tg_refresh`）时，Webhook 触发会全部命中，单规则菜单命令失去意义。改为：菜单命令和盘级命令都通过管理接口按规则 ID 执行，事件名只保留给 `/refresh`（全盘）和 `/run`（高级）使用。完成回执按规则 ID 轮询，同样精确。

## v0.7：菜单自动映射（2026-08-12）

自动发现的结果之前只在 `/list` 里显示，没进 Telegram 命令菜单。这一版调用 `setMyCommands` 把每条规则注册成 `/refresh_<规则>`：命令名由规则名转成拼音 slug（Telegram 命令名不允许中文，剧集→juji、电影→dianying），描述显示后台规则全名。启动、`/info` 和定时任务都会刷新菜单，镜像内置 pypinyin。

## v0.6：工程化交付（2026-08-12）

项目定名 LitePan-TGBot，推送到 GitHub，新增 GitHub Actions 自动构建 Docker 镜像并推送 GHCR（`ghcr.io/MbAIGC/litepan-tgbot`），标签含 latest、v*、sha-*。

## v0.5：隐私优化（2026-08-12）

密码和 API Key 只在本机与 LitePan 之间传递，不经过 Telegram，也不出现在回复和日志里。回复默认不显示 LitePan 地址（`show_url` 开关），保留盘名、规则名、任务名。

## v0.4：自动发现（2026-08-12）

之前盘名和事件名要靠手工在配置里抄一遍，容易和后台对不上。这一版让机器人登录 LitePan 后自动读取账号列表、STRM/整理任务、自动化规则，生成“盘名 → 规则”映射：`/refresh <盘名>` 直接用账号名，`/info` 直接列出规则。手工 `DRIVES` 映射降级为可选项。

## v0.3：明确每用户独立部署模型（2026-08-12）

确认场景是每个用户私有部署自己的 LitePan 和 Bot（用户 A 有 litepan-A + bot-A，用户 B 有 litepan-B + bot-B），代码里零硬编码，盘名、任务名、事件名全部来自各自配置。`users.json` 支持单条目为主，同时保留多条目能力。

## v0.2：多盘命令与自动化模型澄清（2026-08-11）

通过阅读 LitePan 源码确认：STRM 任务按“账号 + 目录”配置，自动化规则的动作链挂具体任务，Webhook 请求里的 path 只参与规则过滤、不传给动作。据此明确 `/refresh` 不带路径不等于全盘刷新，全盘与否取决于规则动作链里挂了哪些任务；支持 `/refresh <盘名>` 按别名触发。

## v0.1：基础版（2026-08-11）

最初的独立 Bot：零改动 LitePan 源码，通过开放接口 `POST /api/open/automation/events` 触发自动化，支持 `/refresh`、`/run`、`/ping`、`/status` 等命令；触发回执默认有，完成回执通过管理员账号轮询任务状态推送。纯 Python 3 标准库实现，支持 Docker 部署。
