#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LitePan Telegram Bot —— 独立进程，零改动 LitePan 源码，支持多用户。

原理：
  Telegram 消息 -> 本 Bot（长轮询 getUpdates）
  -> POST {LITEPAN_URL}/api/open/automation/events（Authorization: Bearer <API Key>）
  -> LitePan 自动化规则（Webhook 触发）异步执行动作链

多用户：users.json 按 chat_id 绑定各自的 LitePan 实例与「盘名 -> 事件」映射；
未配置 users.json 时回退到 .env 的单用户模式（LITEPAN_URL / LITEPAN_API_KEY ...）。

仅依赖 Python 3 标准库，无需 pip 安装任何第三方包。

运行：
  python3 tgbot.py --check   # 检查配置（不启动轮询）
  python3 tgbot.py           # 启动 Bot
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

log = logging.getLogger("litepan-tgbot")

try:
    from pypinyin import lazy_pinyin

    _PINYIN_AVAILABLE = True
except ImportError:
    lazy_pinyin = None
    _PINYIN_AVAILABLE = False


class ConfigError(Exception):
    pass


class TgError(Exception):
    def __init__(self, code, description):
        super().__init__("Telegram API %s: %s" % (code, description))
        self.code = code
        self.description = description


class LitePanError(Exception):
    pass


def _env(name, default=None, required=False):
    val = os.getenv(name)
    if val is None or val.strip() == "":
        if required:
            raise ConfigError("缺少环境变量 %s" % name)
        return default
    return val.strip()


def _env_int(name, default):
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("环境变量 %s 必须是整数，当前值: %s" % (name, raw))


def _env_drives(name):
    """解析「盘名 -> 事件」映射：支持 '别名:事件,别名:事件' 或 JSON 对象两种格式。"""
    raw = _env(name, "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}
        except Exception:
            raise ConfigError("%s 不是合法的 JSON 对象" % name)
    drives = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        sep = ":" if ":" in item else ("：" if "：" in item else None)
        if sep is None:
            raise ConfigError("%s 格式错误：应为「别名:事件名,别名:事件名」，当前项: %s" % (name, item))
        alias, event = item.split(sep, 1)
        alias, event = alias.strip(), event.strip()
        if alias and event:
            drives[alias] = event
    return drives


def _slugify(name):
    """把名称转成 Telegram 命令可用的 slug（小写字母/数字/下划线）。

    Telegram 命令名不允许中文，中文部分转拼音（剧集 -> juji）；
    未安装 pypinyin 时退化为仅保留 ASCII 部分。
    """
    if _PINYIN_AVAILABLE:
        name = "".join(lazy_pinyin(name))
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _safe_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"success": False, "message": raw[:300]}


def _http_json(url, method="GET", data=None, headers=None, timeout=15):
    """发送 HTTP 请求并解析 JSON；网络异常抛 ConnectionError。"""
    req_headers = {
        "User-Agent": "litepan-tgbot/1.0",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _safe_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _safe_json(raw)
    except urllib.error.URLError as e:
        raise ConnectionError("网络错误: %s" % e.reason)
    except (socket.timeout, TimeoutError) as e:
        raise ConnectionError("网络超时: %s" % e)
    except OSError as e:
        raise ConnectionError("网络错误: %s" % e)


class UserProfile:
    """单个 Telegram 会话（或一组会话）绑定的 LitePan 配置。"""

    def __init__(
        self,
        chat_ids,
        lite_url,
        api_key,
        default_event="tg_refresh",
        source="telegram",
        default_path="/",
        message="",
        drives=None,
        admin_user="",
        admin_password="",
        lite_timeout=15,
        receipt_poll=5,
        receipt_timeout=1800,
        show_url=False,
    ):
        self.chat_ids = [int(c) for c in chat_ids]
        self.lite_url = lite_url.rstrip("/")
        self.api_key = api_key
        self.default_event = _normalize_event(default_event)
        self.source = source
        self.default_path = default_path
        self.message = message
        self.drives = {str(k).strip(): str(v).strip() for k, v in (drives or {}).items() if k and v}
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.lite_timeout = lite_timeout
        self.receipt_poll = receipt_poll
        self.receipt_timeout = receipt_timeout
        self.show_url = show_url

    @property
    def receipt_enabled(self):
        return bool(self.admin_user and self.admin_password)

    def masked_key(self):
        if len(self.api_key) <= 8:
            return "****"
        return self.api_key[:4] + "****" + self.api_key[-4:]

    def lookup_drive(self, alias):
        """按盘名/规则别名（忽略大小写）查事件名，未命中返回 None。"""
        target = alias.strip().lower()
        for name, event in self.drives.items():
            if name.strip().lower() == target:
                return event
        return None

    def drive_list_text(self):
        if not self.drives:
            return "（未配置盘名映射）"
        return "、".join("%s → %s" % (name, event) for name, event in self.drives.items())

    def describe(self):
        lines = []
        if self.show_url:
            lines.append("LitePan URL : %s" % self.lite_url)
        lines += [
            "默认规则   : %s (source=%s, 默认路径=%s)" % (
                self.default_event or "自动（见 /list）", self.source, self.default_path),
            "API Key    : %s" % self.masked_key(),
            "规则别名   : %s" % self.drive_list_text(),
            "回执模式   : %s" % ("开启（管理员轮询）" if self.receipt_enabled else "关闭"),
        ]
        if self.message:
            lines.append("附带消息   : %s" % self.message)
        return "\n".join(lines)

    @staticmethod
    def from_dict(raw):
        chat_ids = raw.get("chat_ids") or raw.get("chat_id") or []
        if isinstance(chat_ids, (int, str)):
            chat_ids = [chat_ids]
        if not chat_ids:
            raise ConfigError("users.json 中存在没有 chat_ids 的条目")
        lite_url = str(raw.get("litepan_url") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        if not lite_url or not api_key:
            raise ConfigError("users.json 条目（chat_ids=%s）缺少 litepan_url 或 api_key" % chat_ids)
        return UserProfile(
            chat_ids=chat_ids,
            lite_url=lite_url,
            api_key=api_key,
            default_event=_normalize_event(str(raw.get("default_event") or "").strip()),
            source=str(raw.get("source") or "telegram").strip(),
            default_path=str(raw.get("default_path") or "/").strip(),
            message=str(raw.get("message") or "").strip(),
            drives=raw.get("drives") or {},
            admin_user=str(raw.get("admin_user") or "").strip(),
            admin_password=str(raw.get("admin_password") or "").strip(),
            lite_timeout=int(raw.get("lite_timeout") or 15),
            receipt_poll=int(raw.get("receipt_poll") or 5),
            receipt_timeout=int(raw.get("receipt_timeout") or 1800),
            show_url=bool(raw.get("show_url", False)),
        )

    @staticmethod
    def from_env():
        """单用户兼容：读取 .env 中的 LitePan 配置。"""
        return UserProfile(
            chat_ids=[],
            lite_url=_env("LITEPAN_URL", required=True),
            api_key=_env("LITEPAN_API_KEY", required=True),
            default_event=_normalize_event(_env("LITEPAN_EVENT", "tg_refresh")),
            source=_env("LITEPAN_SOURCE", "telegram"),
            default_path=_env("LITEPAN_DEFAULT_PATH", "/"),
            message=_env("LITEPAN_MESSAGE", ""),
            drives=_env_drives("DRIVES"),
            admin_user=_env("LITEPAN_ADMIN_USER", ""),
            admin_password=_env("LITEPAN_ADMIN_PASSWORD", ""),
            lite_timeout=_env_int("LITEPAN_TIMEOUT", 15),
            receipt_poll=_env_int("TG_RECEIPT_POLL_SECONDS", 5),
            receipt_timeout=_env_int("TG_RECEIPT_TIMEOUT_SECONDS", 1800),
            show_url=_env("SHOW_LITEPAN_URL", "0").lower() in ("1", "true", "yes", "on"),
        )


def _normalize_event(value):
    """'auto' / 空 表示默认规则由自动发现决定。"""
    value = value.strip()
    if value.lower() in ("", "auto"):
        return ""
    return value


class Config:
    def __init__(self):
        self.bot_token = _env("TG_BOT_TOKEN", required=True)
        self.api_base = _env("TG_API_BASE", "https://api.telegram.org").rstrip("/")
        self.poll_timeout = _env_int("TG_POLL_TIMEOUT", 30)
        allowed = _env("TG_ALLOWED_IDS", "")
        self._allowed_ids_env = {int(x) for x in allowed.split(",") if x.strip()} if allowed else set()
        self.state_file = _env("TG_STATE_FILE", "tgbot-state.json")
        self.users_file = _env("USERS_FILE", "users.json")
        self.menu_refresh_minutes = _env_int("TG_MENU_REFRESH_MINUTES", 5)

        self.profiles = {}
        self.fallback = None
        self._load_profiles()

        if not self.profiles and self.fallback is None:
            raise ConfigError(
                "未找到任何 LitePan 配置：请配置 users.json（USERS_FILE），"
                "或使用 .env 单用户模式（LITEPAN_URL / LITEPAN_API_KEY）"
            )
        if not 1 <= self.poll_timeout <= 300:
            raise ConfigError("TG_POLL_TIMEOUT 需在 1~300 之间")
        if not 1 <= self.menu_refresh_minutes <= 1440:
            raise ConfigError("TG_MENU_REFRESH_MINUTES 需在 1~1440 之间")

    def _load_profiles(self):
        if os.path.isfile(self.users_file):
            try:
                with open(self.users_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise ConfigError("读取 %s 失败: %s" % (self.users_file, e))
            raw_users = data.get("users") if isinstance(data, dict) else data
            if not isinstance(raw_users, list):
                raise ConfigError("%s 格式错误：应为 {\"users\": [...]}" % self.users_file)
            for raw in raw_users:
                profile = UserProfile.from_dict(raw)
                for cid in profile.chat_ids:
                    self.profiles[cid] = profile
            log.info("已加载 %d 个用户配置（%s）", len(self.profiles), self.users_file)
        elif _env("LITEPAN_URL", "") and _env("LITEPAN_API_KEY", ""):
            self.fallback = UserProfile.from_env()
            log.info("未发现 %s，使用 .env 单用户模式", self.users_file)

    @property
    def allowed_ids(self):
        if self._allowed_ids_env:
            return self._allowed_ids_env
        return set(self.profiles.keys())

    def profile_for(self, chat_id):
        return self.profiles.get(int(chat_id), self.fallback)

    def check_summary(self):
        lines = [
            "TG API     : %s" % self.api_base,
            "状态文件   : %s" % self.state_file,
            "用户配置   : %s" % self.users_file,
        ]
        if self.profiles:
            for cid in sorted(self.profiles):
                p = self.profiles[cid]
                lines.append("  chat %s -> %s（默认规则 %s，%d 个别名）" % (
                    cid, p.lite_url, p.default_event, len(p.drives)))
        if self.fallback:
            lines.append("  （兼容模式）env -> %s" % self.fallback.lite_url)
        if self._allowed_ids_env:
            lines.append("会话白名单 : %s" % ", ".join(str(x) for x in sorted(self._allowed_ids_env)))
        else:
            lines.append("会话白名单 : 由 users.json 的 chat_ids 决定")
        return "\n".join(lines)


class Discovery:
    """从 LitePan 管理接口读取账号、STRM 任务、自动化规则，自动生成盘名 -> 事件映射。"""

    def __init__(self, profile):
        self.profile = profile
        self.accounts = {}       # account_id -> 账号名（如 光鸭-A）
        self.strm_tasks = {}     # strm task_id -> {name, account_id}
        self.organize_tasks = {} # organize task_id -> {name, account_id}
        self.rules = []          # webhook 规则：{id, name, event, tasks:[], accounts:[]}
        self.by_event = {}       # event -> [规则]
        self.by_account = {}     # account_id -> set(event)（仅单账号规则）
        self.slugs = {}          # slug（如 gy01） -> 账号名（如 GY01）
        self.account_slug = {}   # 账号名 -> slug
        self.rule_by_slug = {}   # slug -> 规则 {id, name, event, tasks}

    def fetch(self):
        client = LitePanClient(self.profile)
        for acc in client.admin_get("/api/admin/accounts"):
            self.accounts[int(acc.get("id") or 0)] = str(acc.get("name") or "").strip()
        options = {}
        try:
            options = client.admin_get("/api/admin/automation/options") or {}
        except LitePanError:
            options = {}
        for t in options.get("strm_tasks") or []:
            self.strm_tasks[int(t.get("id") or 0)] = {
                "name": str(t.get("name") or "").strip(),
                "account_id": int(t.get("account_id") or 0),
            }
        for t in options.get("organize_tasks") or []:
            self.organize_tasks[int(t.get("id") or 0)] = {
                "name": str(t.get("name") or "").strip(),
                "account_id": int(t.get("account_id") or 0),
            }
        for r in client.admin_get("/api/admin/automation/rules"):
            if r.get("trigger_type") != "webhook":
                continue
            ev = str((r.get("trigger_config") or {}).get("event") or "").strip()
            if not ev:
                continue
            task_labels = []
            task_accounts = set()
            for a in r.get("actions") or []:
                atype = a.get("type")
                kind = None
                if atype in ("strm", "strm_scrape"):
                    kind = "strm"
                elif atype == "organize":
                    kind = "organize"
                if kind:
                    tid = int((a.get("params") or {}).get("task_id") or 0)
                    if tid:
                        task_labels.append(self._task_label(kind, tid))
                        acc_id = (self.strm_tasks if kind == "strm" else self.organize_tasks).get(tid, {}).get("account_id")
                        if acc_id:
                            task_accounts.add(acc_id)
            info = {
                "id": int(r.get("id") or 0),
                "name": str(r.get("name") or "").strip() or ("规则#%s" % r.get("id")),
                "event": ev,
                "tasks": task_labels,
                "accounts": sorted(task_accounts),
            }
            self.rules.append(info)
            self.by_event.setdefault(ev, []).append(info)
            # 只把“全部任务都属于同一账号”的规则算作该账号的单盘规则，
            # 避免 /refresh 光鸭-A 误触发挂多账号任务的“全盘”规则。
            if len(task_accounts) == 1:
                self.by_account.setdefault(next(iter(task_accounts)), set()).add(ev)
        self._build_slugs()
        self._build_rule_slugs()

    def _build_slugs(self):
        used = set()
        pan_i = 0
        for aid in sorted(self.by_account, key=lambda a: self.accounts.get(a, "")):
            name = self.accounts.get(aid, "")
            slug = _slugify(name)
            if not slug:
                pan_i += 1
                slug = "pan%d" % pan_i
            base, i = slug, 1
            while slug in used:
                i += 1
                slug = "%s_%d" % (base, i)
            used.add(slug)
            self.slugs[slug] = name
            self.account_slug[name] = slug

    def _build_rule_slugs(self):
        """按规则名生成菜单命令 slug，重名自动加 _2/_3 后缀。"""
        used = set()
        for r in sorted(self.rules, key=lambda x: x["id"]):
            slug = _slugify(r["name"])
            if not slug:
                slug = "rule_%s" % r["id"]
            base, i = slug, 1
            while slug in used:
                i += 1
                slug = "%s_%d" % (base, i)
            used.add(slug)
            r["slug"] = slug
            self.rule_by_slug[slug] = r

    def _task_label(self, kind, task_id):
        t = (self.strm_tasks if kind == "strm" else self.organize_tasks).get(task_id)
        if t and t["name"]:
            return t["name"]
        return "%s任务#%s" % (kind, task_id)

    def account_rules(self, name):
        """按账号名查该账号的单盘规则：先精确匹配，再唯一子串匹配。"""
        target = name.strip().lower()
        exact = [aid for aid, n in self.accounts.items() if n.lower() == target]
        ids = set(exact)
        if not ids:
            subs = [aid for aid, n in self.accounts.items() if target and (target in n.lower() or n.lower() in target)]
            if len(subs) != 1:
                return []
            ids = set(subs)
        return [r for r in self.rules if len(r["accounts"]) == 1 and r["accounts"][0] in ids]

    def account_events(self, name):
        return sorted(set(r["event"] for r in self.account_rules(name)))


class TelegramBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.offset = self._load_offset()
        self.stop = threading.Event()
        self._discovery_cache = {}
        self._discovery_ttl = 60
        self._last_menu_refresh = 0.0

    def tg_call(self, method, params):
        url = "%s/bot%s/%s" % (self.cfg.api_base, self.cfg.bot_token, method)
        try:
            _, body = _http_json(url, method="POST", data=params, timeout=self.cfg.poll_timeout + 10)
        except ConnectionError as e:
            raise TgError(0, str(e))
        if isinstance(body, dict) and body.get("ok"):
            return body.get("result")
        code = body.get("error_code") if isinstance(body, dict) else None
        desc = body.get("description") if isinstance(body, dict) else str(body)
        raise TgError(code or 0, desc or "未知错误")

    def say(self, chat_id, text):
        try:
            self.tg_call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        except TgError as e:
            log.warning("sendMessage 失败 chat=%s: %s", chat_id, e)

    def _load_offset(self):
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                return int(json.load(f).get("offset", 0))
        except Exception:
            return 0

    def _save_offset(self):
        try:
            with open(self.cfg.state_file, "w", encoding="utf-8") as f:
                json.dump({"offset": self.offset}, f)
        except Exception as e:
            log.warning("保存状态文件失败: %s", e)

    def run(self):
        log.info("Bot 启动：%d 个用户配置", len(self.cfg.profiles) + (1 if self.cfg.fallback else 0))
        log.info("命令菜单拼音支持：%s", "开启" if _PINYIN_AVAILABLE else "关闭（纯中文规则名用编号兜底）")
        self.refresh_menu()
        self._last_menu_refresh = time.time()
        while not self.stop.is_set():
            if self._menu_due():
                self.refresh_menu()
            try:
                updates = self.tg_call(
                    "getUpdates",
                    {"offset": self.offset, "timeout": self.cfg.poll_timeout, "allowed_updates": ["message"]},
                )
            except TgError as e:
                if e.code == 401:
                    log.error("Telegram token 无效，退出")
                    return
                if e.code == 409:
                    log.error("409 冲突：已有另一个实例在轮询 getUpdates，请确保单实例运行")
                    return
                log.warning("getUpdates 失败: %s", e)
                time.sleep(10)
                continue
            except Exception as e:
                log.warning("getUpdates 异常: %s", e)
                time.sleep(10)
                continue
            if not updates:
                continue
            for u in updates:
                self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                if "message" in u:
                    try:
                        self.handle_message(u["message"])
                    except Exception as e:
                        log.exception("处理消息失败: %s", e)
            self._save_offset()

    @staticmethod
    def split_command(text):
        parts = text.split(None, 1)
        cmd = (parts[0] if parts else "").lower().split("@")[0]
        arg = (parts[1] if len(parts) > 1 else "").strip()
        return cmd, arg

    def help_text(self, profile=None):
        default_event = (profile.default_event if profile else "") or "自动（见 /list）"
        return "\n".join(
            [
                "LitePan TG Bot 可用命令：",
                "/refresh          触发默认规则（全盘）：%s" % default_event,
                "/refresh <盘名>   触发指定盘，如 /refresh 光鸭-A（盘名取自 LitePan 账号）",
                "/refresh_<规则>   菜单里按规则名生成的快捷命令（自动更新）",
                "/refresh /路径    触发默认规则，路径仅作记录",
                "/list             自动列出本会话 LitePan 的账号、规则与事件",
                "/run <事件> [路径] 触发任意 Webhook 事件",
                "/ping             检查本会话 LitePan 连通性",
                "/status           查看本会话绑定配置与连通性",
                "/start /help      显示本帮助",
            ]
        )

    def handle_message(self, msg):
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return
        if self.cfg.allowed_ids and chat_id not in self.cfg.allowed_ids:
            log.warning("拒绝未授权会话 %s", chat_id)
            self.say(chat_id, "未授权：请将你的 chat_id 加入白名单（TG_ALLOWED_IDS 或 users.json 的 chat_ids）。")
            return
        profile = self.cfg.profile_for(chat_id)
        if profile is None:
            self.say(chat_id, "该会话未绑定 LitePan 实例：请在 %s 中为该 chat_id 配置，或在 .env 中配置 LITEPAN_URL。" % self.cfg.users_file)
            return
        cmd, arg = self.split_command(text)
        if cmd in ("/start", "/help"):
            self.say(chat_id, self.help_text(profile))
        elif cmd == "/ping":
            self.ping(chat_id, profile)
        elif cmd == "/status":
            self.status(chat_id, profile)
        elif cmd == "/list":
            self.say(chat_id, self.list_text(chat_id, profile))
            self.refresh_menu(profile)
        elif cmd in ("/refresh", "/strm"):
            self.refresh(chat_id, profile, arg)
        elif cmd.startswith("/refresh_"):
            self.refresh_slug(chat_id, profile, cmd[len("/refresh_"):])
        elif cmd == "/run":
            parts = arg.split(None, 1)
            if not parts:
                self.say(chat_id, "用法：/run <事件名> [路径]，例如 /run quark01_refresh /")
                return
            event = parts[0]
            path = parts[1] if len(parts) > 1 else profile.default_path
            self.trigger_and_report(chat_id, profile, event, profile.source, path)
        else:
            self.say(chat_id, self.help_text(profile))

    def refresh_slug(self, chat_id, profile, slug):
        """处理菜单生成的 /refresh_<规则> 命令：按规则 ID 精确执行，避免同名事件误触发。"""
        d = self._discovery(chat_id, profile)
        if d is None:
            self.say(chat_id, "未找到盘名命令「/refresh_%s」，可先 /list 查看。" % slug)
            return
        rule = d.rule_by_slug.get(slug)
        if rule is not None:
            self.run_rule_and_report(chat_id, profile, rule)
            return
        account = d.slugs.get(slug)
        if account is not None:
            rules = d.account_rules(account)
            if rules:
                self.run_rules_and_report(chat_id, profile, rules)
                return
            self.say(chat_id, "账号「%s」没有可触发的单盘规则。" % account)
            return
        self.say(chat_id, "未找到规则命令「/refresh_%s」，可先 /list 查看。" % slug)

    def run_rule_and_report(self, chat_id, profile, rule):
        """精确执行单条规则（管理接口按规则 ID）；无管理员账号时退回事件触发。"""
        if not profile.receipt_enabled:
            self.trigger_and_report(chat_id, profile, rule["event"], profile.source, profile.default_path)
            return
        try:
            LitePanClient(profile).run_rule(rule["id"])
        except LitePanError as e:
            self.say(chat_id, "⚠️ 规则「%s」提交失败：%s\n可改用 /run %s 触发。" % (rule["name"], e, rule["event"]))
            return
        self.say(chat_id, "✅ 已提交执行规则：「%s」\n任务异步执行中。" % rule["name"])
        threading.Thread(
            target=self.watch_run,
            args=(chat_id, profile, rule["id"], rule["name"]),
            daemon=True,
        ).start()

    def run_rules_and_report(self, chat_id, profile, rules):
        """精确执行账号下的多条单盘规则；无管理员账号时退回按事件触发。"""
        if not profile.receipt_enabled:
            events = sorted(set(r["event"] for r in rules))
            for ev in events:
                self.trigger_and_report(chat_id, profile, ev, profile.source, profile.default_path)
            return
        ok_names, failed = [], []
        for rule in rules:
            try:
                LitePanClient(profile).run_rule(rule["id"])
                ok_names.append(rule["name"])
                threading.Thread(
                    target=self.watch_run,
                    args=(chat_id, profile, rule["id"], rule["name"]),
                    daemon=True,
                ).start()
            except LitePanError as e:
                failed.append((rule["name"], str(e)))
        if ok_names:
            self.say(chat_id, "✅ 已提交 %d 条规则执行：%s\n任务异步执行中。" % (
                len(ok_names), "、".join("「%s」" % n for n in ok_names)))
        for name, err in failed:
            self.say(chat_id, "⚠️ 规则「%s」提交失败：%s" % (name, err))

    def refresh(self, chat_id, profile, arg):
        """/refresh 不带参数=默认规则（全盘）；带 /路径=默认规则；带盘名=对应规则。"""
        if not arg:
            events = self.default_events(chat_id, profile)
            if not events:
                self.say(
                    chat_id,
                    "未配置默认规则：可在配置里设置 default_event，或先 /list 查看规则后用 /refresh <盘名> 或 /run <事件>。",
                )
                return
            for ev in events:
                self.trigger_and_report(chat_id, profile, ev, profile.source, profile.default_path)
            return
        if arg.startswith("/"):
            events = self.default_events(chat_id, profile)
            if not events:
                self.say(chat_id, "未配置默认规则，请用 /run <事件> %s" % arg)
                return
            for ev in events:
                self.trigger_and_report(chat_id, profile, ev, profile.source, arg)
            return
        ev = profile.lookup_drive(arg)
        if ev:
            self.trigger_and_report(chat_id, profile, ev, profile.source, profile.default_path)
            return
        d = self._discovery(chat_id, profile)
        rules = d.account_rules(arg) if d else []
        if not rules:
            hint = "可先 /list 查看 LitePan 里的盘名。"
            if not profile.receipt_enabled:
                hint = "未配置管理员账号，无法自动读取盘名；可配置 DRIVES 映射或在 users.json 填 admin 账号。" + hint
            self.say(chat_id, "未找到盘名「%s」。%s" % (arg, hint))
            return
        self.run_rules_and_report(chat_id, profile, rules)

    def default_events(self, chat_id, profile):
        if profile.default_event:
            return [profile.default_event]
        d = self._discovery(chat_id, profile)
        if d:
            events = sorted(set(r["event"] for r in d.rules))
            if len(events) == 1:
                return events
        return []

    def _discovery(self, chat_id, profile):
        if not profile.receipt_enabled:
            return None
        now = time.time()
        cached = self._discovery_cache.get(chat_id)
        if cached is not None and now - cached[0] < self._discovery_ttl:
            return cached[1]
        d = Discovery(profile)
        try:
            d.fetch()
            self._discovery_cache[chat_id] = (now, d)
            return d
        except LitePanError as e:
            log.warning("自动发现失败 chat=%s: %s", chat_id, e)
            self._discovery_cache[chat_id] = (now, None)
            return None

    def list_text(self, chat_id, profile):
        d = self._discovery(chat_id, profile)
        lines = []
        if profile.show_url:
            lines.append("本会话 LitePan：%s" % profile.lite_url)
        if d is None:
            lines.append("默认规则（/refresh）：%s" % (profile.default_event or "未配置"))
            lines.append("盘名映射（DRIVES）：%s" % profile.drive_list_text())
            if not profile.receipt_enabled:
                lines.append("提示：配置管理员账号（admin_user/admin_password）后，可自动读取 LitePan 的账号与规则，无需手工维护盘名映射。")
            return "\n".join(lines)
        if profile.default_event:
            lines.append("默认规则（/refresh）：%s" % profile.default_event)
        elif len(d.rules) == 1:
            lines.append("默认规则（/refresh）：%s（唯一规则，自动采用）" % d.rules[0]["event"])
        else:
            lines.append("默认规则（/refresh）：未指定，请用 /refresh <盘名> 或 /run <事件>")
        if d.rules:
            lines.append("")
            lines.append("规则（事件 → 名称 → 涉及任务）：")
            for r in d.rules:
                task_part = "任务：" + "、".join(r["tasks"]) if r["tasks"] else "（未挂任务）"
                cmd = "（/refresh_%s）" % r.get("slug", "") if r.get("slug") else ""
                lines.append("  %s → 「%s」%s，%s" % (r["event"], r["name"], cmd, task_part))
        else:
            lines.append("没有发现 Webhook 自动化规则，请先在 LitePan 后台创建。")
        account_ids = sorted(d.by_account, key=lambda aid: d.accounts.get(aid, str(aid)))
        if account_ids:
            lines.append("")
            lines.append("盘名（/refresh 可用）：")
            for aid in account_ids:
                name = d.accounts.get(aid, aid)
                lines.append("  %s → %s" % (name, "、".join(sorted(d.by_account[aid]))))
        if profile.drives:
            lines.append("")
            lines.append("手动覆盖（DRIVES）：%s" % profile.drive_list_text())
        if profile.receipt_enabled:
            lines.append("")
            lines.append("提示：/refresh_<规则> 与 /refresh <盘名> 按规则精确执行；同名通知事件只影响 /refresh 与 /run。")
        return "\n".join(lines)

    def ping(self, chat_id, profile):
        try:
            LitePanClient(profile).health()
            self.say(chat_id, "✅ LitePan 连接正常")
        except LitePanError as e:
            self.say(chat_id, "⚠️ %s" % e)

    def status(self, chat_id, profile):
        try:
            LitePanClient(profile).health()
            status = "✅ LitePan 连接正常"
        except LitePanError as e:
            status = "⚠️ %s" % e
        discovery = "开启（自动读取账号/规则）" if self._discovery(chat_id, profile) is not None else "关闭（需管理员账号）"
        self.say(chat_id, profile.describe() + "\n自动发现: %s\n%s" % (discovery, status))

    def refresh_menu(self, profile=None):
        """把发现的规则映射成 Telegram 命令菜单（setMyCommands）。"""
        commands = [
            {"command": "start", "description": "帮助"},
            {"command": "refresh", "description": "触发默认规则（全盘）"},
            {"command": "list", "description": "查看账号与规则"},
            {"command": "run", "description": "触发任意事件，如 /run 事件名"},
            {"command": "ping", "description": "连通性检查"},
            {"command": "status", "description": "查看配置"},
        ]
        profiles = []
        if profile is not None:
            profiles.append(profile)
        else:
            seen = set()
            for p in list(self.cfg.profiles.values()) + ([self.cfg.fallback] if self.cfg.fallback else []):
                if id(p) not in seen:
                    seen.add(id(p))
                    profiles.append(p)
        for p in profiles:
            chat_id = p.chat_ids[0] if p.chat_ids else 0
            d = self._discovery(chat_id, p)
            if d:
                for slug, rule in d.rule_by_slug.items():
                    commands.append({"command": "refresh_%s" % slug, "description": rule["name"]})
        try:
            self.tg_call("setMyCommands", {"commands": commands})
            self._last_menu_refresh = time.time()
            log.info("菜单已更新：%d 个命令", len(commands))
        except TgError as e:
            log.warning("更新菜单失败: %s", e)

    def _menu_due(self):
        return time.time() - self._last_menu_refresh >= self.cfg.menu_refresh_minutes * 60

    def trigger_and_report(self, chat_id, profile, event, source, path):
        try:
            data = LitePanClient(profile).trigger(event, source, path)
        except LitePanError as e:
            self.say(chat_id, "⚠️ 触发失败：%s" % e)
            return
        matched = int(data.get("matched") or 0)
        triggered = data.get("triggered") or []
        if matched == 0:
            self.say(
                chat_id,
                "未匹配到任何 Webhook 自动化规则。请在 LitePan「任务管理-自动联动」新建规则："
                "触发方式=Webhook，事件名=%s，再试一次。" % event,
            )
            return
        names = "、".join("「%s」" % t.get("name", "") for t in triggered)
        self.say(chat_id, "✅ 已触发 %d 条规则：%s\n任务异步执行中。" % (matched, names))
        if profile.receipt_enabled and triggered:
            for t in triggered:
                threading.Thread(
                    target=self.watch_run,
                    args=(chat_id, profile, t.get("id"), t.get("name") or ""),
                    daemon=True,
                ).start()

    def watch_run(self, chat_id, profile, rule_id, rule_name):
        client = LitePanClient(profile)
        try:
            base = 0
            for r in client.list_runs(rule_id, 5):
                base = max(base, int(r.get("id") or 0))
        except LitePanError as e:
            self.say(chat_id, "⚠️ 回执模式初始化失败：%s" % e)
            return
        deadline = time.time() + profile.receipt_timeout
        while time.time() < deadline:
            try:
                runs = client.list_runs(rule_id, 5)
            except LitePanError as e:
                log.warning("轮询任务状态失败 rule=%s: %s", rule_id, e)
                time.sleep(profile.receipt_poll)
                continue
            for r in runs:
                rid = int(r.get("id") or 0)
                if rid > base and r.get("status") != "running":
                    self.say(chat_id, self.render_result(rule_name, r))
                    return
            time.sleep(profile.receipt_poll)
        self.say(chat_id, "⏱️ 等待规则「%s」执行结果超时（%ds）。" % (rule_name, profile.receipt_timeout))

    @staticmethod
    def render_result(rule_name, run):
        status = run.get("status") or "unknown"
        head = "✅" if status == "success" else "❌"
        lines = [
            "%s 规则「%s」执行完成" % (head, rule_name),
            "状态：%s" % status,
            "运行ID：%s" % run.get("id"),
        ]
        msg = (run.get("message") or "").strip()
        if msg:
            lines.append("消息：%s" % msg)
        steps = (run.get("result") or {}).get("steps") or []
        if steps:
            labels = []
            for s in steps:
                name = s.get("name") or s.get("type") or "?"
                mark = "✓" if s.get("status") == "success" else "✗"
                labels.append("%s%s" % (name, mark))
            lines.append("步骤：" + " / ".join(labels))
        return "\n".join(lines)


class LitePanClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(self, path, method="GET", data=None, headers=None):
        url = self.cfg.lite_url + path
        hdrs = dict(headers or {})
        body = None
        if data is not None:
            if hdrs.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                body = urllib.parse.urlencode(data).encode("utf-8")
            else:
                body = json.dumps(data).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=self.cfg.lite_timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _safe_json(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _safe_json(raw)
        except urllib.error.URLError as e:
            raise LitePanError("无法连接 LitePan: %s" % e.reason)
        except (socket.timeout, TimeoutError) as e:
            raise LitePanError("连接 LitePan 超时: %s" % e)
        except OSError as e:
            raise LitePanError("无法连接 LitePan: %s" % e)

    def trigger(self, event, source, path):
        payload = {"event": event, "source": source, "path": path}
        if self.cfg.message:
            payload["message"] = self.cfg.message
        status, body = self.request(
            "/api/open/automation/events",
            method="POST",
            data=payload,
            headers={"Authorization": "Bearer %s" % self.cfg.api_key},
        )
        if status >= 400 or not body.get("success"):
            raise LitePanError(body.get("message") if isinstance(body, dict) else "HTTP %d" % status)
        return body.get("data") or {}

    def health(self):
        status, body = self.request("/api/health")
        if status >= 400 or not (isinstance(body, dict) and body.get("success")):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError(msg)
        return body.get("data") or {}

    def login(self):
        status, body = self.request(
            "/api/auth/login",
            method="POST",
            data={"username": self.cfg.admin_user, "password": self.cfg.admin_password, "remember": "0"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status >= 400 or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError("管理员登录失败：%s" % msg)

    def admin_get(self, path):
        """管理接口 GET，401 时自动重新登录再试一次。"""
        status, body = self.request(path)
        if status == 401 and self.cfg.receipt_enabled:
            self.login()
            status, body = self.request(path)
        if status >= 400 or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError(msg)
        return body.get("data") or []

    def run_rule(self, rule_id):
        """按规则 ID 精确执行（管理接口），401 时自动重新登录再试一次。"""
        path = "/api/admin/automation/rules/%s/run" % rule_id
        status, body = self.request(path, method="POST")
        if status == 401 and self.cfg.receipt_enabled:
            self.login()
            status, body = self.request(path, method="POST")
        if status >= 400 or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError(msg)
        return body.get("data") or {}

    def list_runs(self, rule_id, limit=5):
        qs = urllib.parse.urlencode({"rule_id": rule_id, "limit": limit})
        return self.admin_get("/api/admin/automation/runs?%s" % qs)


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = Config()
    except ConfigError as e:
        print("配置错误：%s" % e, file=sys.stderr)
        sys.exit(2)
    if "--check" in sys.argv:
        print(cfg.check_summary())
        return
    bot = TelegramBot(cfg)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop.set()
        log.info("收到中断，退出")


if __name__ == "__main__":
    main()
