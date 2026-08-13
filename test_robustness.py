#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LitePan-TGBot 健壮性测试：配置校验、游标 at-least-once、回执去重、slug 冲突、规则归属、终态与 /info 文案。"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tgbot


def make_profile(**kw):
    base = dict(
        chat_ids=[123456789],
        lite_url="http://A-NAS:5211",
        api_key="lpk_api_xxxA",
        default_event="tg_refresh",
        admin_user="adminA",
        admin_password="pwA",
    )
    base.update(kw)
    return tgbot.UserProfile(**base)


def make_cfg(profile):
    class FakeCfg:
        api_base = "x"
        bot_token = "x"
        poll_timeout = 30
        state_file = os.path.join(tempfile.mkdtemp(), "state.json")
        allowed_ids = {123456789}
        menu_refresh_minutes = 30
        profiles = {123456789: profile}
        fallback = None

        def profile_for(self, cid):
            return self.profiles.get(cid)

        def check_summary(self):
            return ""

    return FakeCfg()


def base_user(**over):
    data = {
        "chat_ids": [1],
        "litepan_url": "http://x:5211",
        "api_key": "lpk_api_x",
        "lite_timeout": 15,
        "receipt_poll": 5,
        "receipt_timeout": 1800,
        "show_url": False,
    }
    data.update(over)
    return data


def expect_config_error(fn, needle):
    try:
        fn()
    except tgbot.ConfigError as e:
        assert needle in str(e), (needle, str(e))
        return
    raise SystemExit("应当抛 ConfigError（包含 %r）" % needle)


def with_env(**values):
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return saved


def restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def main():
    # ---- 1. _parse_bool 严格解析 ----
    assert tgbot._parse_bool("false", "x") is False
    assert tgbot._parse_bool("1", "x") is True
    assert tgbot._parse_bool(0, "x") is False
    assert tgbot._parse_bool(True, "x") is True
    assert tgbot._parse_bool("on", "x") is True
    for bad in ("", "abc", "2", 2, 1.5):
        expect_config_error(lambda b=bad: tgbot._parse_bool(b, "show_url"), "show_url")

    # ---- 2. users.json 字段边界校验 ----
    expect_config_error(lambda: tgbot.UserProfile.from_dict(base_user(lite_timeout=0)), "lite_timeout")
    expect_config_error(lambda: tgbot.UserProfile.from_dict(base_user(receipt_poll=0)), "receipt_poll")
    expect_config_error(lambda: tgbot.UserProfile.from_dict(base_user(receipt_timeout=3)), "receipt_timeout")
    expect_config_error(lambda: tgbot.UserProfile.from_dict(base_user(chat_ids=[1, 1])), "chat_ids")
    expect_config_error(lambda: tgbot.UserProfile.from_dict(base_user(chat_ids="abc")), "chat_ids")
    expect_config_error(lambda: tgbot.UserProfile.from_dict(base_user(show_url="abc")), "show_url")
    # show_url 字符串布尔值：false 不能解析成 True
    assert tgbot.UserProfile.from_dict(base_user(show_url="false")).show_url is False
    assert tgbot.UserProfile.from_dict(base_user(show_url="true")).show_url is True

    # ---- 3. TG_ALLOWED_IDS 非法值 -> ConfigError ----
    saved_env = with_env(
        TG_BOT_TOKEN="1:abc",
        TG_ALLOWED_IDS="abc",
        USERS_FILE=os.path.join(tempfile.mkdtemp(), "nope.json"),
        LITEPAN_URL=None,
        LITEPAN_API_KEY=None,
    )
    try:
        expect_config_error(lambda: tgbot.Config(), "TG_ALLOWED_IDS")
    finally:
        restore_env(saved_env)

    # ---- 4. users.json 存在但为空 -> ConfigError（不静默回退 .env）----
    tmp = tempfile.mkdtemp()
    users_path = os.path.join(tmp, "users.json")
    with open(users_path, "w", encoding="utf-8") as f:
        json.dump({"users": []}, f)
    saved_env = with_env(
        TG_BOT_TOKEN="1:abc",
        TG_ALLOWED_IDS="1",
        USERS_FILE=users_path,
        LITEPAN_URL=None,
        LITEPAN_API_KEY=None,
    )
    try:
        expect_config_error(lambda: tgbot.Config(), "没有用户条目")
    finally:
        restore_env(saved_env)

    # ---- 5. 游标：处理成功才推进 + 原子落盘 ----
    profile = make_profile()
    cfg = make_cfg(profile)
    bot = tgbot.TelegramBot(cfg)
    handled = []
    bot.handle_message = lambda msg: handled.append(msg)
    bot._process_updates([
        {"update_id": 1, "message": {"chat": {"id": 123456789}, "text": "/info"}},
        {"update_id": 2},  # 非消息更新直接跳过
    ])
    assert bot.offset == 3, bot.offset
    assert len(handled) == 1
    with open(cfg.state_file, "r", encoding="utf-8") as f:
        assert json.load(f) == {"offset": 3}
    assert not os.path.exists(cfg.state_file + ".tmp")

    # ---- 6. 游标：消息处理失败重试耗尽后跳过（不卡死轮询）----
    bot2 = tgbot.TelegramBot(make_cfg(profile))

    def boom(_msg):
        raise RuntimeError("boom")

    bot2.handle_message = boom
    orig_sleep = time.sleep
    time.sleep = lambda _s: None
    try:
        bot2._process_updates([{"update_id": 5, "message": {"chat": {"id": 123456789}, "text": "x"}}])
    finally:
        time.sleep = orig_sleep
    assert bot2.offset == 6, bot2.offset

    # ---- 7. 回执：非终态不回执；同规则多个运行各自回执一次 ----
    class FakeClient:
        def __init__(self, runs):
            self.runs = runs

        def list_runs(self, rule_id, limit=5):
            return self.runs

    bot3 = tgbot.TelegramBot(make_cfg(profile))
    msgs = []
    bot3.say = lambda _chat, text: msgs.append(text)
    # queued 不是终态：轮询到超时也不发“完成”回执
    tiny = make_profile(receipt_poll=0.005, receipt_timeout=0.05)
    bot_tiny = tgbot.TelegramBot(make_cfg(tiny))
    msgs_tiny = []
    bot_tiny.say = lambda _chat, text: msgs_tiny.append(text)
    bot_tiny.watch_run(123456789, tiny, 1, "R1", pre_base=0,
                       client=FakeClient([{"id": 9, "status": "queued", "message": "", "result": {}}]))
    assert msgs_tiny and "仍在执行或排队" in msgs_tiny[0], msgs_tiny
    # 同规则两个运行：第一个 running、第二个 success -> 回执第二个
    bot3.watch_run(123456789, profile, 1, "R1", pre_base=0,
                   client=FakeClient([
                       {"id": 9, "status": "running"},
                       {"id": 10, "status": "success", "message": "ok", "result": {}},
                   ]))
    assert len(msgs) == 1 and "运行ID：10" in msgs[0], msgs
    # 第二个线程随后看到第一个运行完成 -> 也回执一次，且不重复回执第二个
    bot3.watch_run(123456789, profile, 1, "R1", pre_base=0,
                   client=FakeClient([
                       {"id": 9, "status": "success", "message": "", "result": {}},
                       {"id": 10, "status": "success", "message": "ok", "result": {}},
                   ]))
    assert len(msgs) == 2 and "运行ID：9" in msgs[1], msgs
    # 已回执过的运行不会重复回执（等到超时也不会再发）
    bot4 = tgbot.TelegramBot(make_cfg(tiny))
    msgs4 = []
    bot4.say = lambda _chat, text: msgs4.append(text)
    bot4._receipted_runs.add((1, 10))
    bot4.watch_run(123456789, tiny, 1, "R1", pre_base=0,
                   client=FakeClient([{"id": 10, "status": "success", "message": "ok", "result": {}}]))
    assert all("执行完成" not in m for m in msgs4), msgs4

    # ---- 8. slug 冲突：账号与规则同名时各自保留入口 ----
    class CollideLite:
        def admin_get(self, path):
            if path == "/api/admin/accounts":
                return [{"id": 1, "name": "剧集"}]
            if path == "/api/admin/automation/options":
                return {"strm_tasks": [{"id": 101, "name": "剧集任务", "account_id": 1}], "organize_tasks": []}
            if path == "/api/admin/automation/rules":
                return [{
                    "id": 1, "name": "剧集", "trigger_type": "webhook",
                    "trigger_config": {"event": "juji"},
                    "actions": [{"type": "strm", "params": {"task_id": 101}}],
                }]
            raise AssertionError("意外接口: %s" % path)

    orig_client = tgbot.LitePanClient
    tgbot.LitePanClient = lambda _profile: CollideLite()
    try:
        d = tgbot.Discovery(make_profile())
        d.fetch()
    finally:
        tgbot.LitePanClient = orig_client
    assert d.rule_by_slug["juji"]["name"] == "剧集"  # 规则保留原名
    assert "juji" not in d.slugs and d.slugs["juji_2"] == "剧集"  # 账号自动加后缀
    assert len(d.account_rules("剧集")) == 1

    # ---- 9. 规则归属：未知任务/其他动作不算单盘规则 ----
    class UnknownTaskLite:
        def admin_get(self, path):
            if path == "/api/admin/accounts":
                return [{"id": 1, "name": "GY01"}]
            if path == "/api/admin/automation/options":
                return {"strm_tasks": [{"id": 101, "name": "GY01-剧集", "account_id": 1}], "organize_tasks": []}
            if path == "/api/admin/automation/rules":
                return [
                    {"id": 1, "name": "正常", "trigger_type": "webhook", "trigger_config": {"event": "a"},
                     "actions": [{"type": "strm", "params": {"task_id": 101}}]},
                    {"id": 2, "name": "未知任务", "trigger_type": "webhook", "trigger_config": {"event": "b"},
                     "actions": [{"type": "strm", "params": {"task_id": 999}}]},
                    {"id": 3, "name": "其他动作", "trigger_type": "webhook", "trigger_config": {"event": "c"},
                     "actions": [{"type": "emby", "params": {}}]},
                ]
            raise AssertionError("意外接口: %s" % path)

    tgbot.LitePanClient = lambda _profile: UnknownTaskLite()
    try:
        d2 = tgbot.Discovery(make_profile())
        d2.fetch()
    finally:
        tgbot.LitePanClient = orig_client
    assert d2.by_account.get(1) == {"a"}, d2.by_account
    assert len(d2.rules) == 3

    # ---- 10. 自动发现字段容错：缺字段/脏字段不抛异常 ----
    class MinimalLite:
        def admin_get(self, path):
            if path == "/api/admin/accounts":
                return [{"id": 1, "name": "GY01"}]
            if path == "/api/admin/automation/options":
                return {}  # 缺 strm_tasks / organize_tasks
            if path == "/api/admin/automation/rules":
                return [{"id": 1, "trigger_type": "webhook", "trigger_config": {"event": "x"},
                         "actions": [{"type": "strm", "params": {"task_id": 101}}]}]
            raise AssertionError("意外接口: %s" % path)

    tgbot.LitePanClient = lambda _profile: MinimalLite()
    try:
        d3 = tgbot.Discovery(make_profile())
        d3.fetch()
    finally:
        tgbot.LitePanClient = orig_client
    assert d3.accounts == {1: "GY01"} and len(d3.rules) == 1
    assert d3.rules[0]["name"] == "规则#1" and d3.rules[0]["tasks"] == []

    # ---- 11. /info 文案：自动发现失败与未开启区分 ----
    class FailLite:
        def health(self):
            return {"status": "ok"}

        def admin_get(self, path):
            raise tgbot.LitePanError("boom")

    tgbot.LitePanClient = lambda _profile: FailLite()
    try:
        bot5 = tgbot.TelegramBot(make_cfg(profile))
        bot5.tg_call = lambda _method, _params: None
        msgs5 = []
        bot5.say = lambda _chat, text: msgs5.append(text)
        bot5.handle_message({"chat": {"id": 123456789}, "text": "/info"})
        text = msgs5[-1]
        assert "自动发现失败" in text, text
        assert "自动发现未开启" not in text
    finally:
        tgbot.LitePanClient = orig_client

    print("ROBUST_TESTS_PASSED")


if __name__ == "__main__":
    main()
