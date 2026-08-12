#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LitePan-TGBot 离线测试：命令路由、自动发现、菜单映射、隐私开关。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tgbot


def make_profile():
    return tgbot.UserProfile(
        chat_ids=[123456789],
        lite_url="http://A-NAS:5211",
        api_key="lpk_api_xxxA",
        default_event="tg_refresh",
        admin_user="adminA",
        admin_password="pwA",
    )


class StubLite:
    triggers = []

    def __init__(self, profile):
        pass

    def health(self):
        return {"status": "ok"}

    def trigger(self, event, source, path):
        StubLite.triggers.append((event, source, path))
        return {"matched": 1, "triggered": [{"id": 7, "name": "GY01-剧集"}]}

    def list_runs(self, rule_id, limit=5):
        return []

    def admin_get(self, path):
        if path == "/api/admin/accounts":
            return [
                {"id": 1, "name": "GY01"},
                {"id": 2, "name": "123-A"},
                {"id": 3, "name": "光鸭"},
            ]
        if path == "/api/admin/automation/options":
            return {
                "strm_tasks": [
                    {"id": 101, "name": "GY01-剧集", "account_id": 1},
                    {"id": 102, "name": "123-A-综艺", "account_id": 2},
                    {"id": 103, "name": "光鸭任务", "account_id": 3},
                ],
                "organize_tasks": [],
                "emby": {},
            }
        if path == "/api/admin/automation/rules":
            return [
                {
                    "id": 1,
                    "name": "GY01-剧集",
                    "trigger_type": "webhook",
                    "trigger_config": {"event": "tg_refresh"},
                    "actions": [{"type": "strm", "params": {"task_id": 101}}],
                },
                {
                    "id": 2,
                    "name": "123A刷新",
                    "trigger_type": "webhook",
                    "trigger_config": {"event": "123A_refresh"},
                    "actions": [{"type": "strm", "params": {"task_id": 102}}],
                },
                {
                    "id": 3,
                    "name": "光鸭刷新",
                    "trigger_type": "webhook",
                    "trigger_config": {"event": "gy_refresh"},
                    "actions": [{"type": "strm", "params": {"task_id": 103}}],
                },
            ]
        raise AssertionError("意外接口: %s" % path)


def make_cfg(profile):
    class FakeCfg:
        api_base = "x"
        bot_token = "x"
        poll_timeout = 30
        state_file = "/tmp/lpbot-state.json"
        allowed_ids = {123456789}
        profiles = {123456789: profile}
        fallback = None

        def profile_for(self, cid):
            return self.profiles.get(cid)

        def check_summary(self):
            return ""

    return FakeCfg()


def main():
    tgbot.LitePanClient = StubLite
    profile = make_profile()
    menu_calls = []
    messages = []

    bot = tgbot.TelegramBot(make_cfg(profile))

    def fake_tg(method, params):
        if method == "setMyCommands":
            menu_calls.append(params)
        return None

    bot.tg_call = fake_tg
    bot.say = lambda chat, text: messages.append((chat, text))

    def send(text):
        bot.handle_message({"chat": {"id": 123456789}, "text": text})

    # 自动发现：账号、任务、规则、slug
    d = bot._discovery(123456789, profile)
    assert d.slugs == {"gy01": "GY01", "123_a": "123-A", "pan1": "光鸭"}, d.slugs
    assert d.account_events("GY01") == ["tg_refresh"]
    assert d.account_events("123-A") == ["123A_refresh"]

    # 菜单映射
    bot.refresh_menu(profile)
    cmds = menu_calls[-1]["commands"]
    names = [c["command"] for c in cmds]
    desc = {c["command"]: c["description"] for c in cmds}
    assert "refresh" in names and "list" in names and "start" in names
    assert "refresh_gy01" in names and "refresh_123_a" in names and "refresh_pan1" in names
    assert desc["refresh_gy01"] == "刷新 GY01"

    # /list 显示快捷命令
    send("/list")
    text = messages[-1][1]
    assert "（/refresh_gy01）" in text and "（/refresh_123_a）" in text and "（/refresh_pan1）" in text, text

    # 菜单命令触发
    send("/refresh_gy01")
    assert StubLite.triggers[-1] == ("tg_refresh", "telegram", "/"), StubLite.triggers
    send("/refresh_123_a")
    assert StubLite.triggers[-1] == ("123A_refresh", "telegram", "/"), StubLite.triggers
    send("/refresh_pan1")
    assert StubLite.triggers[-1] == ("gy_refresh", "telegram", "/"), StubLite.triggers

    # 未知 slug
    before = len(StubLite.triggers)
    send("/refresh_unknown")
    assert "未找到盘名命令" in messages[-1][1]
    assert len(StubLite.triggers) == before

    # 启动时刷新菜单（无参数，遍历配置）
    menu_calls.clear()
    bot2 = tgbot.TelegramBot(make_cfg(profile))
    bot2.tg_call = fake_tg
    bot2.refresh_menu()
    assert any(c["command"] == "refresh_gy01" for c in menu_calls[-1]["commands"])

    print("MENU_TESTS_PASSED")


if __name__ == "__main__":
    main()
