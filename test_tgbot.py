#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LitePan-TGBot 离线测试：命令路由、自动发现、规则菜单映射、定时刷新、隐私开关。"""

import os
import sys
import time

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
    runs = []

    def __init__(self, profile):
        pass

    def health(self):
        return {"status": "ok"}

    def trigger(self, event, source, path):
        StubLite.triggers.append((event, source, path))
        return {"matched": 1, "triggered": [{"id": 7, "name": "GY01-剧集"}]}

    def run_rule(self, rule_id):
        StubLite.runs.append(rule_id)
        return {"rule_id": rule_id, "submitted": True, "trigger_source": "manual"}

    def list_runs(self, rule_id, limit=5):
        return []

    def admin_get(self, path):
        if path == "/api/admin/accounts":
            return [
                {"id": 1, "name": "GY01"},
                {"id": 2, "name": "光鸭"},
            ]
        if path == "/api/admin/automation/options":
            return {
                "strm_tasks": [
                    {"id": 101, "name": "GY01-剧集", "account_id": 1},
                    {"id": 102, "name": "GY01-电影", "account_id": 1},
                    {"id": 103, "name": "光鸭任务", "account_id": 2},
                ],
                "organize_tasks": [],
                "emby": {},
            }
        if path == "/api/admin/automation/rules":
            return [
                {
                    "id": 1,
                    "name": "AM-GY01-剧集",
                    "trigger_type": "webhook",
                    "trigger_config": {"event": "tg_refresh"},
                    "actions": [{"type": "strm", "params": {"task_id": 101}}],
                },
                {
                    "id": 2,
                    "name": "AM-GY01-电影",
                    "trigger_type": "webhook",
                    "trigger_config": {"event": "gy_movie"},
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
        menu_refresh_minutes = 30
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
    assert d.slugs == {"gy01": "GY01", "guangya": "光鸭"}, d.slugs
    assert d.account_events("GY01") == ["gy_movie", "tg_refresh"]
    assert d.account_events("光鸭") == ["gy_refresh"]
    # 规则 slug：中文转拼音（剧集->juji、电影->dianying、光鸭刷新->guangyashuaxin）
    assert set(d.rule_by_slug) == {"am_gy01_juji", "am_gy01_dianying", "guangyashuaxin"}, d.rule_by_slug
    assert d.rule_by_slug["am_gy01_juji"]["name"] == "AM-GY01-剧集"
    assert d.rule_by_slug["am_gy01_dianying"]["name"] == "AM-GY01-电影"
    assert d.rule_by_slug["guangyashuaxin"]["name"] == "光鸭刷新"

    # 菜单映射：静态命令 + 按规则名生成命令，描述显示后台规则名；/run 在最后
    bot.refresh_menu(profile)
    cmds = menu_calls[-1]["commands"]
    names = [c["command"] for c in cmds]
    desc = {c["command"]: c["description"] for c in cmds}
    assert "refresh" in names and "info" in names and "menu" in names and "start" in names
    assert names[-1] == "run", names
    assert desc["refresh"] == "触发所有规则"
    assert "refresh_am_gy01_juji" in names and "refresh_am_gy01_dianying" in names
    assert "refresh_guangyashuaxin" in names
    assert desc["refresh_am_gy01_juji"] == "AM-GY01-剧集"
    assert desc["refresh_am_gy01_dianying"] == "AM-GY01-电影"

    # 菜单变更检测：内容未变时不再调用 setMyCommands
    bot.refresh_menu(profile)
    assert len(menu_calls) == 1
    # /menu 强制刷新
    send("/menu")
    assert len(menu_calls) == 2
    assert "命令菜单已更新" in messages[-1][1]

    # /info（含 /list /status /ping 别名）显示连接状态、配置、规则、盘名
    send("/list")
    text = messages[-1][1]
    assert "✅ LitePan 连接正常" in text and "自动发现: 开启" in text, text
    assert "（/refresh_am_gy01_juji）" in text and "（/refresh_am_gy01_dianying）" in text, text
    assert "「AM-GY01-剧集」" in text and "「AM-GY01-电影」" in text, text
    assert "按规则精确执行" in text, text
    send("/status")
    assert messages[-1][1] == text
    send("/ping")
    assert messages[-1][1] == text
    send("/info")
    assert messages[-1][1] == text
    assert "手动映射(DRIVES)" not in text  # 未配置 DRIVES 时不显示

    # 按规则命令触发：走管理接口按规则 ID 精确执行，不再按事件（避免同名事件全部触发）
    send("/refresh_am_gy01_juji")
    assert StubLite.runs[-1] == 1 and not StubLite.triggers, (StubLite.runs, StubLite.triggers)
    assert "已提交执行规则：「AM-GY01-剧集」" in messages[-1][1]
    send("/refresh_am_gy01_dianying")
    assert StubLite.runs[-1] == 2 and not StubLite.triggers, (StubLite.runs, StubLite.triggers)
    assert "已提交执行规则：「AM-GY01-电影」" in messages[-1][1]
    send("/refresh_guangyashuaxin")
    assert StubLite.runs[-1] == 3 and not StubLite.triggers

    # 旧账号级 slug 兜底：/refresh_gy01 精确执行该盘全部单盘规则（两条规则 ID）
    StubLite.runs.clear()
    send("/refresh_gy01")
    assert StubLite.runs == [1, 2] and not StubLite.triggers, (StubLite.runs, StubLite.triggers)
    assert "已提交 2 条规则执行" in messages[-1][1]

    # 未知 slug
    before = len(StubLite.runs)
    send("/refresh_unknown")
    assert "未找到规则命令" in messages[-1][1]
    assert len(StubLite.runs) == before

    # /refresh 触发所有规则（按规则 ID 精确执行全部规则）
    StubLite.runs.clear()
    StubLite.triggers.clear()
    send("/refresh")
    assert StubLite.runs == [1, 2, 3] and not StubLite.triggers, (StubLite.runs, StubLite.triggers)
    assert "已提交 3 条规则执行" in messages[-1][1]

    # /refresh /路径 不再支持
    send("/refresh /Movies")
    assert "路径参数已不再支持" in messages[-1][1]
    assert StubLite.runs == [1, 2, 3]

    # 定时刷新：到期才刷，刷新后重置计时
    bot._last_menu_refresh = time.time() - 1801
    assert bot._menu_due() is True
    menu_calls.clear()
    bot.refresh_menu(profile)  # 内容未变 -> 不调用 API，但更新计时
    assert len(menu_calls) == 0
    assert bot._menu_due() is False

    # 启动时刷新菜单（无参数，遍历配置）
    menu_calls.clear()
    bot2 = tgbot.TelegramBot(make_cfg(profile))
    bot2.tg_call = fake_tg
    bot2.refresh_menu()
    assert any(c["command"] == "refresh_am_gy01_juji" for c in menu_calls[-1]["commands"])

    # 无 pypinyin 时退化：只保留 ASCII
    orig = tgbot._PINYIN_AVAILABLE
    tgbot._PINYIN_AVAILABLE = False
    try:
        assert tgbot._slugify("AM-GY01-剧集") == "am_gy01"
        assert tgbot._slugify("GY01") == "gy01"
    finally:
        tgbot._PINYIN_AVAILABLE = orig

    print("MENU_TESTS_PASSED")


if __name__ == "__main__":
    main()
