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
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

log = logging.getLogger("litepan-tgbot")
_AUTH_LOCK = threading.Lock()

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


def _env_int(name, default, minimum=None):
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("环境变量 %s 必须是整数，当前值: %s" % (name, raw))
    if minimum is not None and value < minimum:
        raise ConfigError("环境变量 %s 必须 >= %d，当前值: %d" % (name, minimum, value))
    return value


def _parse_int(value, name, minimum=None):
    """解析配置里的整数；非法值或低于下限时抛 ConfigError。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ConfigError("%s 必须是整数，当前值: %r" % (name, value))
    if minimum is not None and value < minimum:
        raise ConfigError("%s 必须 >= %d，当前值: %d" % (name, minimum, value))
    return value


def _raw_value(raw, key, default):
    """取 JSON 配置字段：缺失或空白字符串视为未设置（保留 0 等合法值）。"""
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return value


def _parse_bool(value, name):
    """严格解析布尔配置：true/false、1/0、yes/no、on/off；非法值抛 ConfigError。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    raise ConfigError("%s 必须是 true/false/1/0/yes/no/on/off，当前值: %r" % (name, value))


def _int_field(obj, key, default=0):
    """容错解析接口字段为整数：缺失或类型异常时返回默认值。"""
    try:
        return int(obj.get(key) or default)
    except (TypeError, ValueError):
        return default


def _str_field(obj, key, default=""):
    """容错解析接口字段为字符串：缺失或类型异常时返回默认值。"""
    try:
        value = obj.get(key)
        if value is None:
            return str(default)
        return str(value).strip() or str(default)
    except (TypeError, ValueError):
        return str(default)


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


MAX_SLUG_LEN = 24
MAX_MENU_COMMANDS = 100
MAX_MESSAGE_LEN = 4000
TERMINAL_STATUSES = frozenset({"success", "failed", "error", "cancelled"})


def _fit_slug(slug, used, max_len=MAX_SLUG_LEN):
    """保证 slug 不超长且唯一：'refresh_' 前缀 + slug 总长不超过 32 字符。"""
    slug = slug[:max_len]
    base, i = slug, 1
    while slug in used:
        i += 1
        suffix = "_%d" % i
        slug = base[: max_len - len(suffix)] + suffix
    used.add(slug)
    return slug


def _chunk_text(text, limit=MAX_MESSAGE_LEN):
    """按行把长文本切成不超过 limit 的片段（Telegram 单条消息上限 4096）。"""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = ""
        cur = cur + ("\n" if cur else "") + line
    if cur:
        chunks.append(cur)
    return chunks


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
        if receipt_timeout < receipt_poll:
            raise ConfigError(
                "receipt_timeout (%d) 必须 >= receipt_poll (%d)" % (receipt_timeout, receipt_poll)
            )

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
            "默认事件   : %s (source=%s, 默认路径=%s)" % (
                self.default_event or "auto", self.source, self.default_path),
            "API Key    : %s" % self.masked_key(),
            "回执模式   : %s" % ("开启（管理员轮询）" if self.receipt_enabled else "关闭"),
        ]
        if self.drives:
            lines.append("手动映射(DRIVES) : %s" % self.drive_list_text())
        if self.message:
            lines.append("附带消息   : %s" % self.message)
        return "\n".join(lines)

    @staticmethod
    def from_dict(raw):
        chat_ids = raw.get("chat_ids") or raw.get("chat_id") or []
        if isinstance(chat_ids, str):
            chat_ids = re.split(r"[,，\s]+", chat_ids)
        elif isinstance(chat_ids, (int, float)):
            chat_ids = [chat_ids]
        parsed = []
        for c in chat_ids:
            try:
                parsed.append(int(str(c).strip()))
            except (TypeError, ValueError):
                raise ConfigError("users.json 条目 chat_ids 含非法值: %r" % (c,))
        if not parsed:
            raise ConfigError("users.json 中存在没有 chat_ids 的条目")
        if len(set(parsed)) != len(parsed):
            raise ConfigError("users.json 条目 chat_ids 不能重复: %r" % (parsed,))
        chat_ids = parsed
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
            lite_timeout=_parse_int(_raw_value(raw, "lite_timeout", 15), "lite_timeout", minimum=1),
            receipt_poll=_parse_int(_raw_value(raw, "receipt_poll", 5), "receipt_poll", minimum=1),
            receipt_timeout=_parse_int(_raw_value(raw, "receipt_timeout", 1800), "receipt_timeout", minimum=1),
            show_url=_parse_bool(raw.get("show_url", False), "show_url"),
        )

    @staticmethod
    def from_env():
        """单用户兼容：读取 .env 中的 LitePan 配置。"""
        return UserProfile(
            chat_ids=[],
            lite_url=_env("LITEPAN_URL", required=True),
            api_key=_env("LITEPAN_API_KEY", required=True),
            # 兼容旧配置：优先读新名，未设置时回退旧名 LITEPAN_EVENT
            default_event=_normalize_event(_env("LITEPAN_FALLBACK_EVENT", _env("LITEPAN_EVENT", "tg_refresh"))),
            source=_env("LITEPAN_SOURCE", "telegram"),
            default_path=_env("LITEPAN_DEFAULT_PATH", "/"),
            message=_env("LITEPAN_MESSAGE", ""),
            drives=_env_drives("DRIVES"),
            admin_user=_env("LITEPAN_ADMIN_USER", ""),
            admin_password=_env("LITEPAN_ADMIN_PASSWORD", ""),
            lite_timeout=_env_int("LITEPAN_TIMEOUT", 15, minimum=1),
            receipt_poll=_env_int("TG_RECEIPT_POLL_SECONDS", 5, minimum=1),
            receipt_timeout=_env_int("TG_RECEIPT_TIMEOUT_SECONDS", 1800, minimum=1),
            show_url=_parse_bool(_env("SHOW_LITEPAN_URL", "0"), "SHOW_LITEPAN_URL"),
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
        ids = set()
        for x in allowed.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                ids.add(int(x))
            except ValueError:
                raise ConfigError("TG_ALLOWED_IDS 含非法值: %r（必须为整数 chat_id）" % x)
        self._allowed_ids_env = ids
        self.state_file = _env("TG_STATE_FILE", "tgbot-state.json")
        self.users_file = _env("USERS_FILE", "users.json")
        self.menu_refresh_minutes = _env_int("TG_MENU_REFRESH_MINUTES", 30)

        self.profiles = {}
        self.fallback = None
        self._load_profiles()

        if not self.profiles and self.fallback is None:
            raise ConfigError(
                "未找到任何 LitePan 配置：请配置 users.json（USERS_FILE），"
                "或使用 .env 单用户模式（LITEPAN_URL / LITEPAN_API_KEY）"
            )
        if self.fallback is not None and not self._allowed_ids_env:
            raise ConfigError(
                "单用户 .env 模式必须配置 TG_ALLOWED_IDS 白名单，否则任何人都能使用你的 Bot"
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
            if not raw_users:
                raise ConfigError(
                    "%s 存在但没有用户条目；若要用 .env 单用户模式，请删除或改名该文件" % self.users_file
                )
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
        self.by_account = {}     # account_id -> set(event)（仅单账号规则）
        self.slugs = {}          # slug（如 gy01） -> 账号名（如 GY01）
        self.rule_by_slug = {}   # slug -> 规则 {id, name, event, tasks}

    def fetch(self):
        """从 LitePan 管理接口读取账号、任务、规则；解析异常统一降级为 LitePanError。"""
        try:
            self._fetch()
        except LitePanError:
            raise
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            raise LitePanError("自动发现数据解析失败: %s" % e)

    def _fetch(self):
        client = LitePanClient(self.profile)
        for acc in client.admin_get("/api/admin/accounts"):
            aid = _int_field(acc, "id")
            if aid:
                self.accounts[aid] = _str_field(acc, "name")
        options = {}
        try:
            options = client.admin_get("/api/admin/automation/options") or {}
        except LitePanError:
            options = {}
        for t in options.get("strm_tasks") or []:
            tid = _int_field(t, "id")
            if tid:
                self.strm_tasks[tid] = {
                    "name": _str_field(t, "name"),
                    "account_id": _int_field(t, "account_id"),
                }
        for t in options.get("organize_tasks") or []:
            tid = _int_field(t, "id")
            if tid:
                self.organize_tasks[tid] = {
                    "name": _str_field(t, "name"),
                    "account_id": _int_field(t, "account_id"),
                }
        for r in client.admin_get("/api/admin/automation/rules"):
            if r.get("trigger_type") != "webhook":
                continue
            ev = _str_field(r.get("trigger_config") or {}, "event")
            if not ev:
                continue
            rid = _int_field(r, "id")
            task_labels = []
            task_accounts = set()
            parse_ok = True  # 所有动作均成功解析（类型已知且任务存在）
            for a in r.get("actions") or []:
                atype = a.get("type")
                kind = None
                if atype in ("strm", "strm_scrape"):
                    kind = "strm"
                elif atype == "organize":
                    kind = "organize"
                else:
                    parse_ok = False
                    continue
                tid = _int_field(a.get("params") or {}, "task_id")
                task = (self.strm_tasks if kind == "strm" else self.organize_tasks).get(tid)
                if not tid or task is None:
                    parse_ok = False
                    continue
                task_labels.append(task["name"] or "%s任务#%s" % (kind, tid))
                if task.get("account_id"):
                    task_accounts.add(task["account_id"])
            info = {
                "id": rid,
                "name": _str_field(r, "name") or ("规则#%s" % rid),
                "event": ev,
                "tasks": task_labels,
                "accounts": sorted(task_accounts),
            }
            self.rules.append(info)
            # 只把“全部动作解析成功、且所有任务都属于同一账号”的规则算作单盘规则，
            # 避免 /refresh <盘名> 误触发挂未知任务、其他动作或多账号任务的规则。
            if parse_ok and len(task_accounts) == 1:
                self.by_account.setdefault(next(iter(task_accounts)), set()).add(ev)
        self._build_rule_slugs()
        self._build_slugs()

    def _build_slugs(self):
        """账号 slug：与规则 slug 共用命名空间，冲突时自动加 _2/_3 后缀。"""
        used = set(self.rule_by_slug)
        pan_i = 0
        for aid in sorted(self.by_account, key=lambda a: self.accounts.get(a, "")):
            name = self.accounts.get(aid, "")
            slug = _slugify(name)
            if not slug:
                pan_i += 1
                slug = "pan%d" % pan_i
            slug = _fit_slug(slug, used)
            self.slugs[slug] = name

    def _build_rule_slugs(self):
        """按规则名生成菜单命令 slug：限长 + 重名自动加 _2/_3 后缀。"""
        used = set()
        for r in sorted(self.rules, key=lambda x: x["id"]):
            slug = _slugify(r["name"])
            if not slug:
                slug = "rule_%s" % r["id"]
            slug = _fit_slug(slug, used)
            r["slug"] = slug
            self.rule_by_slug[slug] = r

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
    MAX_MSG_RETRIES = 5   # 单条消息处理失败的最大重试次数
    RETRY_SLEEP = 2       # 消息处理失败后的重试间隔（秒）

    def __init__(self, cfg):
        self.cfg = cfg
        self.offset = self._load_offset()
        self.stop = threading.Event()
        self._discovery_cache = {}
        self._discovery_ttl = 60
        self._discovery_fetch_lock = threading.Lock()
        self._menu_lock = threading.Lock()
        self._last_menu_refresh = 0.0
        self._last_menu_commands = None
        self._receipted_runs = set()   # 已推送过回执的 (rule_id, run_id)
        self._receipt_lock = threading.Lock()

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
        for chunk in _chunk_text(text):
            try:
                self.tg_call("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})
            except TgError as e:
                log.warning("sendMessage 失败 chat=%s: %s", chat_id, e)

    def _load_offset(self):
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                return int(json.load(f).get("offset", 0))
        except Exception:
            return 0

    def _save_offset(self):
        """原子写游标：先写临时文件再替换，进程中途被杀不会损坏状态文件。"""
        try:
            path = self.cfg.state_file
            tmp = "%s.tmp" % path
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"offset": self.offset}, f)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("保存状态文件失败: %s", e)

    def run(self):
        log.info("Bot 启动：%d 个用户配置", len(self.cfg.profiles) + (1 if self.cfg.fallback else 0))
        log.info("命令菜单拼音支持：%s", "开启" if _PINYIN_AVAILABLE else "关闭（纯中文规则名用编号兜底）")
        # 启动时后台刷新菜单，不阻塞长轮询（LitePan 不可达时最多等 lite_timeout 秒）
        threading.Thread(target=self.refresh_menu, daemon=True).start()
        self._last_menu_refresh = time.time()
        try:
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
                        log.error("409 冲突：已有另一个实例在轮询 getUpdates，5 分钟后重试")
                        if self.stop.wait(300):
                            break
                        continue
                    log.warning("getUpdates 失败: %s", e)
                    time.sleep(10)
                    continue
                except Exception as e:
                    log.warning("getUpdates 异常: %s", e)
                    time.sleep(10)
                    continue
                if not updates:
                    continue
                self._process_updates(updates)
        finally:
            self._save_offset()

    def _process_updates(self, updates):
        """逐条处理更新：消息处理成功后才推进 offset 并落盘（at-least-once）。

        单条消息失败会重试，连续失败超过 MAX_MSG_RETRIES 次后跳过并告警，
        避免某条坏消息永久卡住轮询；重启后 Telegram 重推旧消息时，
        已落盘的 offset 会直接跳过，避免重复执行。
        """
        for u in updates:
            uid = int(u.get("update_id", 0))
            if "message" in u:
                handled = False
                for attempt in range(1, self.MAX_MSG_RETRIES + 1):
                    try:
                        self.handle_message(u["message"])
                        handled = True
                        break
                    except Exception as e:
                        log.exception(
                            "处理消息失败 update=%s 第 %d/%d 次: %s",
                            uid, attempt, self.MAX_MSG_RETRIES, e,
                        )
                        if attempt < self.MAX_MSG_RETRIES:
                            time.sleep(self.RETRY_SLEEP)
                if not handled:
                    log.error(
                        "消息 update=%s 连续 %d 次处理失败，已跳过（避免卡死轮询）",
                        uid, self.MAX_MSG_RETRIES,
                    )
            self.offset = max(self.offset, uid + 1)
            self._save_offset()

    @staticmethod
    def split_command(text):
        parts = text.split(None, 1)
        cmd = (parts[0] if parts else "").lower().split("@")[0]
        arg = (parts[1] if len(parts) > 1 else "").strip()
        return cmd, arg

    def help_text(self, profile=None):
        return "\n".join(
            [
                "LitePan TG Bot 可用命令：",
                "/refresh          触发所有规则（全盘）",
                "/refresh <盘名>   触发指定盘，如 /refresh 光鸭-A（盘名取自 LitePan 账号）",
                "/refresh_<规则>   菜单快捷命令，精确触发单条规则",
                "/info             查看连接状态、配置、规则与盘名",
                "/menu             手动更新命令菜单",
                "/run <事件>    高级：触发任意 Webhook 事件（同名事件会全部触发）",
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
        elif cmd in ("/info", "/list", "/status", "/ping"):
            self.say(chat_id, self.info_text(chat_id, profile))
            self.refresh_menu(profile)
        elif cmd == "/menu":
            ok = self.refresh_menu(profile, force=True)
            self.say(chat_id, "✅ 命令菜单已更新。" if ok else "⚠️ 菜单更新失败，请稍后再试。")
        elif cmd in ("/refresh", "/strm"):
            self.refresh(chat_id, profile, arg)
        elif cmd.startswith("/refresh_"):
            self.refresh_slug(chat_id, profile, cmd[len("/refresh_"):])
        elif cmd == "/run":
            parts = arg.split(None, 1)
            if not parts:
                self.say(chat_id, "用法：/run <事件名>，例如 /run quark01_refresh")
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
            self.say(chat_id, "未找到盘名命令「/refresh_%s」，可先 /info 查看。" % slug)
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
        self.say(chat_id, "未找到规则命令「/refresh_%s」，可先 /info 查看。" % slug)

    def run_rule_and_report(self, chat_id, profile, rule):
        """精确执行单条规则（管理接口按规则 ID）；无管理员账号时退回事件触发。"""
        if not profile.receipt_enabled:
            self.trigger_and_report(chat_id, profile, rule["event"], profile.source, profile.default_path)
            return
        client = LitePanClient(profile)
        pre_base = 0
        try:
            pre_base = client.max_run_id()
        except LitePanError as e:
            log.warning("回执快照失败，退化为 0：%s", e)
        try:
            client.run_rule(rule["id"])
        except LitePanError as e:
            self.say(chat_id, "⚠️ 规则「%s」提交失败：%s\n可改用 /run %s 触发。" % (rule["name"], e, rule["event"]))
            return
        self.say(chat_id, "✅ 已提交执行规则：「%s」\n任务异步执行中。" % rule["name"])
        threading.Thread(
            target=self.watch_run,
            args=(chat_id, profile, rule["id"], rule["name"], pre_base, client),
            daemon=True,
        ).start()

    def run_rules_and_report(self, chat_id, profile, rules):
        """精确执行账号下的多条单盘规则；无管理员账号时退回按事件触发。"""
        if not profile.receipt_enabled:
            events = sorted(set(r["event"] for r in rules))
            for ev in events:
                self.trigger_and_report(chat_id, profile, ev, profile.source, profile.default_path)
            return
        pre_base = 0
        try:
            pre_base = LitePanClient(profile).max_run_id()
        except LitePanError as e:
            log.warning("回执快照失败，退化为 0：%s", e)
        ok_names, failed = [], []
        for rule in rules:
            try:
                client = LitePanClient(profile)
                client.run_rule(rule["id"])
                ok_names.append(rule["name"])
                threading.Thread(
                    target=self.watch_run,
                    args=(chat_id, profile, rule["id"], rule["name"], pre_base, client),
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
        """/refresh 触发所有规则；/refresh <盘名> 精确触发指定盘。"""
        if not arg:
            d = self._discovery(chat_id, profile)
            if d is not None:
                if not d.rules:
                    self.say(chat_id, "后台还没有 Webhook 自动化规则，请先在 LitePan「自动联动」里创建。")
                    return
                self.run_rules_and_report(chat_id, profile, d.rules)
                return
            events = self.default_events(chat_id, profile)
            if not events:
                self.say(chat_id, "未配置默认事件：可先 /info 查看规则，再用 /refresh <盘名>、/refresh_<规则> 或 /run <事件>。")
                return
            for ev in events:
                self.trigger_and_report(chat_id, profile, ev, profile.source, profile.default_path)
            return
        if arg.startswith("/"):
            self.say(chat_id, "路径参数已不再支持：/refresh 触发所有规则，/refresh <盘名> 触发指定盘。")
            return
        ev = profile.lookup_drive(arg)
        if ev:
            self.trigger_and_report(chat_id, profile, ev, profile.source, profile.default_path)
            return
        d = self._discovery(chat_id, profile)
        rules = d.account_rules(arg) if d else []
        if not rules:
            hint = "可先 /info 查看 LitePan 里的盘名。"
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
        with self._discovery_fetch_lock:
            cached = self._discovery_cache.get(chat_id)
            if cached is not None and time.time() - cached[0] < self._discovery_ttl:
                return cached[1]
            d = Discovery(profile)
            try:
                d.fetch()
                self._discovery_cache[chat_id] = (time.time(), d, False)
                return d
            except LitePanError as e:
                log.warning("自动发现失败 chat=%s: %s", chat_id, e)
                self._discovery_cache[chat_id] = (time.time(), None, True)
                return None

    def _discovery_failed(self, chat_id):
        """自动发现已尝试但失败（区别于“未开启/无管理员账号”）。"""
        cached = self._discovery_cache.get(chat_id)
        return bool(cached and cached[2])

    def info_text(self, chat_id, profile):
        """/info：连接状态 + 配置摘要 + 自动发现结果（规则、盘名、菜单命令）。"""
        lines = []
        try:
            LitePanClient(profile).health()
            status = "✅ LitePan 连接正常"
        except LitePanError as e:
            status = "⚠️ %s" % e
        d = self._discovery(chat_id, profile)
        if d is not None:
            status_line = "自动发现已开启"
        elif not profile.receipt_enabled:
            status_line = "自动发现未开启（需管理员账号）"
        elif self._discovery_failed(chat_id):
            status_line = "自动发现失败（LitePan 接口异常，可稍后重试）"
        else:
            status_line = "自动发现未开启"
        lines.append("%s · %s" % (status, status_line))
        lines.append("")
        lines.append("📋 配置")
        if profile.show_url:
            lines.append("· LitePan：%s" % profile.lite_url)
        if not profile.receipt_enabled:
            lines.append("· 默认事件：%s（/refresh 兜底）" % (profile.default_event or "auto"))
        lines.append("· 回执模式：%s" % ("开启（管理员轮询）" if profile.receipt_enabled else "关闭"))
        if profile.drives:
            lines.append("· 手动映射：%s" % profile.drive_list_text())
        if d is None:
            if not profile.receipt_enabled:
                lines.append("")
                lines.append("💡 配置管理员账号后，可自动读取盘名和规则；未配置时 /refresh 使用默认事件触发。")
            elif self._discovery_failed(chat_id):
                lines.append("")
                lines.append("💡 自动发现失败：LitePan 接口异常或管理员账号不可用，可稍后重试或发送 /menu 触发刷新。")
            return "\n".join(lines)
        lines.append("")
        if d.rules:
            lines.append("📦 规则（%d 条）" % len(d.rules))
            for r in d.rules:
                task_part = "、".join(r["tasks"]) if r["tasks"] else "无任务"
                cmd = "/refresh_%s" % r.get("slug", "") if r.get("slug") else "/run %s" % r["event"]
                lines.append("· %s（事件 %s）" % (r["name"], r["event"]))
                lines.append("  命令：%s" % cmd)
                lines.append("  任务：%s" % task_part)
        else:
            lines.append("📦 规则（0 条）")
            lines.append("· 后台还没有 Webhook 自动化规则，请先在 LitePan「自动联动」里创建。")
        account_ids = sorted(d.by_account, key=lambda aid: d.accounts.get(aid, str(aid)))
        if account_ids:
            lines.append("")
            lines.append("💾 盘名")
            for aid in account_ids:
                name = d.accounts.get(aid, aid)
                lines.append("· %s：%s" % (name, "、".join(sorted(d.by_account[aid]))))
        lines.append("")
        lines.append("💡 提示")
        lines.append("· /refresh 触发所有规则；/refresh <盘名>、/refresh_<规则> 精确执行；/run 为高级命令，同名事件会全部触发。")
        return "\n".join(lines)

    def refresh_menu(self, profile=None, force=False):
        with self._menu_lock:
            return self._refresh_menu_locked(profile, force)

    def _refresh_menu_locked(self, profile, force):
        """把发现的规则映射成 Telegram 命令菜单（setMyCommands）。

        默认只在命令列表发生变化时才调用 Telegram API，避免无意义的频繁请求；
        force=True 用于 /menu 手动刷新。
        """
        commands = [
            {"command": "start", "description": "帮助"},
            {"command": "refresh", "description": "触发所有规则"},
            {"command": "info", "description": "查看状态、规则与盘名"},
            {"command": "menu", "description": "手动更新命令菜单"},
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
        commands.append({"command": "run", "description": "高级：触发任意事件"})
        if len(commands) > MAX_MENU_COMMANDS:
            log.warning("规则命令超过 %d 条，菜单已截断", MAX_MENU_COMMANDS)
            commands = commands[:MAX_MENU_COMMANDS]
        if not force and commands == self._last_menu_commands:
            self._last_menu_refresh = time.time()
            return True
        try:
            self.tg_call("setMyCommands", {"commands": commands})
        except TgError as e:
            log.warning("更新菜单失败: %s", e)
            return False
        self._last_menu_commands = commands
        self._last_menu_refresh = time.time()
        log.info("菜单已更新：%d 个命令", len(commands))
        return True

    def _menu_due(self):
        return time.time() - self._last_menu_refresh >= self.cfg.menu_refresh_minutes * 60

    def trigger_and_report(self, chat_id, profile, event, source, path):
        pre_base = 0
        if profile.receipt_enabled:
            try:
                pre_base = LitePanClient(profile).max_run_id()
            except LitePanError as e:
                log.warning("回执快照失败，退化为 0：%s", e)
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
                    args=(chat_id, profile, t.get("id"), t.get("name") or "", pre_base),
                    daemon=True,
                ).start()

    def watch_run(self, chat_id, profile, rule_id, rule_name, pre_base=0, client=None):
        """等待触发后新产生的运行并推送回执（每个运行只回执一次）。

        pre_base 是触发前的全局最大运行 ID：触发前先快照，运行创建后其 ID 必然大于
        快照，即使规则秒完成、快照时运行已结束，也能被识别并回执。
        同一规则短时间内触发多次时，各自的新运行只要进入终态就分别回执，
        不会死等最早的那个运行（避免第一个卡住时第二个完成却收不到回执）。
        """
        client = client or LitePanClient(profile)
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
                if rid <= pre_base:
                    continue
                status = (r.get("status") or "").strip().lower()
                if status not in TERMINAL_STATUSES:
                    continue
                key = (rule_id, rid)
                with self._receipt_lock:
                    if key in self._receipted_runs:
                        continue
                    self._receipted_runs.add(key)
                self.say(chat_id, self.render_result(rule_name, r))
                return
            if self.stop.is_set():
                return
            time.sleep(profile.receipt_poll)
        self.say(chat_id, "⏱️ 规则「%s」仍在执行或排队，未在 %d 秒内收到完成回执。" % (rule_name, profile.receipt_timeout))

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
        with _AUTH_LOCK:
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

    def max_run_id(self):
        """当前全局最大运行 ID（运行列表按 ID 倒序，取第一条即可）。"""
        runs = self.admin_get("/api/admin/automation/runs?limit=1")
        if runs:
            return int((runs[0] or {}).get("id") or 0)
        return 0

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
    if "--health" in sys.argv:
        profiles = list(cfg.profiles.values()) + ([cfg.fallback] if cfg.fallback else [])
        if not profiles:
            print("health: 无可用 LitePan 配置", file=sys.stderr)
            sys.exit(1)
        try:
            LitePanClient(profiles[0]).health()
        except LitePanError as e:
            print("health: %s" % e, file=sys.stderr)
            sys.exit(1)
        print("health: ok")
        return
    bot = TelegramBot(cfg)

    def _handle_signal(_signum, _frame):
        bot.stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop.set()
    log.info("Bot 已退出")


if __name__ == "__main__":
    main()
