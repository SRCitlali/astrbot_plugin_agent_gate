# -*- coding: utf-8 -*-
"""告状与冷静期：证据校验、判定、拦截、告警、管理员指令。"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agtest_support import (  # noqa: E402
    FakeContext,
    FakeContextWithPlatforms,
    StubEvent,
    ag,
    attach_memory_defense,
    build_plugin,
    defense_config,
    wl,
)

from astrbot.api.message_components import At, Plain  # noqa: E402
from astrbot_plugin_agent_gate.defense import (  # noqa: E402
    DEFAULT_SETTINGS,
    DefenseStore,
    ban_key,
    evaluate,
    format_duration,
    mask_openid,
    settings_from_config,
    severity_of,
    strip_mentions,
    verify_quote,
)


def _text(result) -> str:
    if result is None:
        return ""
    parts = []
    for comp in getattr(result, "chain", []) or []:
        text = getattr(comp, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts)


def _plugin(*, whitelist_enabled=None, **defense) -> ag.AgentGatePlugin:
    cfg = {}
    cfg.update(wl("S1", "U9"))
    cfg.update(defense_config(**defense))
    if whitelist_enabled is not None:
        cfg["whitelist_enabled"] = whitelist_enabled
    return build_plugin(cfg)


def _call(coro):
    return asyncio.run(coro)


# ══════════════ 纯函数 ══════════════


def test_severity_aliases():
    assert severity_of("severe") == "severe"
    assert severity_of("SEVERE") == "severe"
    assert severity_of("严重") == "severe"
    assert severity_of("中度") == "moderate"
    assert severity_of("轻微") == "mild"
    assert severity_of("") is None
    assert severity_of("随便") is None
    assert severity_of("乱写的词") is None


def test_verify_quote_accepts_exact_text():
    assert verify_quote("你真是个废物，滚", "废物") != ""


def test_verify_quote_ignores_whitespace_and_case():
    assert verify_quote("你 是 个 Idiot", "idiot") != ""


def test_verify_quote_rejects_fabrication():
    assert verify_quote("今天天气不错", "你他妈") == ""
    assert verify_quote("今天天气不错", "") == ""
    assert verify_quote("", "废物") == ""


def test_verify_quote_strips_leading_mention():
    """群消息里用户写了「@机器人 我干你娘」，模型照抄带 @ 也要受理。"""
    assert verify_quote("我干你娘", "@茜特菈莉 我干你娘") != ""
    assert verify_quote("我干你娘", '<@123456> 我干你娘') != ""
    assert verify_quote(
        "我干你娘", '<qqbot-at-user id="0F2A" /> 我干你娘'
    ) != ""


def test_verify_quote_still_rejects_after_mention_strip():
    assert verify_quote("今天天气不错", "@茜特菈莉 你他妈") == ""


def test_strip_mentions_keeps_text():
    assert strip_mentions("@茜特菈莉 我干你娘") == "我干你娘"
    assert strip_mentions('<@123> <qqbot-at-user id="x" /> 你好') == "你好"


def test_evaluate_mild_first_time_alert_only():
    plan = evaluate("mild", 0, DEFAULT_SETTINGS)
    assert plan["ok"] is True
    assert plan["want_ban"] is False
    assert plan["should_alert"] is True
    assert plan["minutes"] == 0


def test_evaluate_mild_second_strike_bans():
    plan = evaluate("mild", 1, DEFAULT_SETTINGS)
    assert plan["escalated"] is True
    assert plan["want_ban"] is True
    assert plan["minutes"] == DEFAULT_SETTINGS["ban_minutes_moderate"]


def test_evaluate_severe_bans_immediately():
    plan = evaluate("severe", 0, DEFAULT_SETTINGS)
    assert plan["want_ban"] is True
    assert plan["minutes"] == DEFAULT_SETTINGS["ban_minutes_severe"]


def test_evaluate_respects_max_minutes():
    settings = dict(DEFAULT_SETTINGS, ban_minutes_severe=9999, max_ban_minutes=30)
    plan = evaluate("severe", 0, settings)
    assert plan["minutes"] == 30


def test_evaluate_rate_limits():
    plan = evaluate(
        "severe", 0, DEFAULT_SETTINGS, alerts_last_hour=99, bans_last_hour=99
    )
    assert plan["should_alert"] is False
    assert plan["should_ban"] is False


def test_evaluate_dry_run_keeps_alert_but_no_ban():
    settings = dict(DEFAULT_SETTINGS, dry_run=True)
    plan = evaluate("severe", 0, settings)
    assert plan["should_alert"] is True
    assert plan["want_ban"] is True
    assert plan["should_ban"] is False
    assert plan["dry_run"] is True


def test_evaluate_invalid_severity():
    assert evaluate("nonsense", 0, DEFAULT_SETTINGS)["ok"] is False


def test_settings_tolerates_dirty_values():
    settings = settings_from_config(
        {
            "enabled": "false",
            "cooldown_minutes": "abc",
            "ban_minutes_severe": -5,
            "messages": "单行文案",
            "strikes_before_ban": 0,
        }
    )
    assert settings["enabled"] is False
    assert settings["cooldown_minutes"] == DEFAULT_SETTINGS["cooldown_minutes"]
    assert settings["ban_minutes_severe"] == 1
    assert settings["messages"] == ["单行文案"]
    assert settings["strikes_before_ban"] == 1


def test_mask_openid_and_duration():
    assert mask_openid("ABCDEFGHIJKL") == "ABCD…IJKL"
    assert mask_openid("SHORT") == "SHORT"
    assert format_duration(10) == "10 分钟"
    assert format_duration(60) == "1 小时"
    assert format_duration(95) == "1 小时 35 分钟"


# ══════════════ 状态存储 ══════════════


def _fake_clock():
    box = {"now": 1_700_000_000.0}
    return box, (lambda: box["now"])


def test_store_ban_expires():
    box, clock = _fake_clock()
    store = DefenseStore(None, clock=clock)
    store.load()
    key = ban_key("qq_official", "U1")
    store.set_ban(key, 10, severity="moderate", by="llm")
    assert store.active_ban(key) is not None
    box["now"] += 11 * 60
    assert store.active_ban(key) is None


def test_store_persist_and_reload():
    with tempfile.TemporaryDirectory() as tmp:
        key = ban_key("qq_official", "U2")
        store = DefenseStore(tmp)
        store.load()
        store.set_ban(key, 15, severity="severe")
        store.set_alert_target(openid="DEV1")
        again = DefenseStore(tmp)
        again.load()
        assert again.active_ban(key) is not None
        assert again.alert_openid == "DEV1"


def test_store_strike_window():
    box, clock = _fake_clock()
    store = DefenseStore(None, clock=clock)
    store.load()
    key = ban_key("qq_official", "U3")
    store.mark_strike(key)
    box["now"] += 3600
    store.mark_strike(key)
    assert store.strike_count(key, 24 * 3600) == 2
    assert store.strike_count(key, 1800) == 1


def test_store_alert_dedupe_and_rates():
    box, clock = _fake_clock()
    store = DefenseStore(None, clock=clock)
    store.load()
    key = ban_key("qq_official", "U4")
    assert store.recently_alerted(key, 900) is False
    store.mark_alert(key)
    assert store.recently_alerted(key, 900) is True
    assert store.alerts_last_hour() == 1
    box["now"] += 3601
    assert store.recently_alerted(key, 900) is False
    assert store.alerts_last_hour() == 0


def test_store_ban_cap():
    store = DefenseStore(None, clock=lambda: 1_700_000_000.0)
    store.load()
    for index in range(5):
        store.set_ban(ban_key("qq_official", f"U{index}"), 1, cap=3)
    assert len(store.list_bans()) == 3


def test_store_evidence_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        store = DefenseStore(tmp)
        store.load()
        store.log_evidence({"hello": "world"})
        path = Path(tmp) / "harassment.jsonl"
        assert path.is_file()
        assert "world" in path.read_text(encoding="utf-8")


# ══════════════ 拦截 ══════════════


def test_ban_key_uses_platform_and_sender():
    assert ban_key("qq_official", "U1") == "qq_official:U1"
    assert ban_key("", "U1") == "U1"


def test_ban_blocks_message():
    plugin = _plugin()
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "S1"), 10)
    event = StubEvent(sender="S1")
    assert plugin._decide(event) == "ban"
    plugin._enforce(event, "waking")
    assert event.is_stopped() is True
    assert _text(event.get_result()) in ["不想说话"]


def test_ban_beats_whitelist_and_energy():
    plugin = _plugin()
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "S1"), 10)
    event = StubEvent(sender="S1")
    plugin._enforce(event, "waking")
    assert event.is_stopped() is True


def test_ban_works_with_whitelist_off():
    plugin = _plugin(whitelist_enabled=False)
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "S1"), 10)
    event = StubEvent(sender="S1")
    plugin._enforce(event, "waking")
    assert event.is_stopped() is True


def test_admin_is_never_banned():
    plugin = _plugin()
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "S1"), 10)
    event = StubEvent(sender="S1", admin=True)
    plugin._enforce(event, "waking")
    assert event.is_stopped() is False


def test_expired_ban_passes():
    box, clock = _fake_clock()
    plugin = _plugin()
    store = attach_memory_defense(plugin, clock=clock)
    store.set_ban(ban_key("qq_official", "S1"), 1)
    box["now"] += 120
    event = StubEvent(sender="S1")
    assert plugin._decide(event) == "pass"
    plugin._enforce(event, "waking")
    assert event.is_stopped() is False


def test_ban_notice_is_rate_limited():
    plugin = _plugin(notify_cooldown_seconds=300)
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "S1"), 10)
    first = StubEvent(sender="S1")
    plugin._enforce(first, "waking")
    assert first.is_stopped() is True
    assert _text(first.get_result()) != ""
    second = StubEvent(sender="S1")
    plugin._enforce(second, "waking")
    assert second.is_stopped() is True
    assert _text(second.get_result()) == ""


# ══════════════ LLM 工具 ══════════════


def test_tool_rejects_fabricated_quote():
    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="今天天气不错")
    reply = _call(
        plugin.report_harassment(
            event, reason="说脏话", severity="severe", quote="你他妈"
        )
    )
    assert reply.startswith("未受理")
    assert plugin._defense_store().list_bans() == []


def test_tool_rejects_invalid_severity():
    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="你是废物")
    reply = _call(
        plugin.report_harassment(event, reason="辱骂", severity="爆炸", quote="废物")
    )
    assert reply.startswith("未受理")


def test_tool_bans_and_logs_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        plugin = _plugin()
        plugin._defense = DefenseStore(tmp)
        plugin._defense.load()
        plugin.context = FakeContext()
        event = StubEvent(
            sender="S1",
            sender_name="坏人",
            message_str="你就是个废物，滚",
            group="G1",
            group_name="测试群",
            umo="qq_official:GroupMessage:G1",
            session="G1",
        )
        reply = _call(
            plugin.report_harassment(
                event, reason="辱骂", severity="severe", quote="废物"
            )
        )
        assert "冷静期" in reply
        bans = plugin._defense_store().list_bans()
        assert len(bans) == 1
        assert bans[0][1]["minutes"] == DEFAULT_SETTINGS["ban_minutes_severe"]
        evidence = (Path(tmp) / "harassment.jsonl").read_text(encoding="utf-8")
        assert "废物" in evidence
        # 群聊：登记 @ 开发者，同时私聊一份证据
        assert event.get_extra(ag.ALERT_PAYLOAD_KEY)
        assert len(plugin.context.sent) == 1
        assert plugin.context.sent[0][0] == "qq_official:FriendMessage:DEVOPENID123456"


def test_tool_private_chat_sends_dm():
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="你个废物", sender_name="坏人")
    reply = _call(
        plugin.report_harassment(event, reason="辱骂", severity="moderate", quote="废物")
    )
    assert "冷静期" in reply
    assert len(plugin.context.sent) == 1
    assert event.get_extra(ag.ALERT_PAYLOAD_KEY) is None


def test_dm_umo_uses_platform_instance_id_from_event():
    """umo 第一段是平台实例 ID（如「茜特菈莉」），不是 qq_official。"""
    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", umo="茜特菈莉:FriendMessage:ABC", session="ABC")
    assert (
        plugin._effective_alert_umo(event)
        == "茜特菈莉:FriendMessage:DEVOPENID123456"
    )


def test_dm_umo_without_event_uses_platform_manager():
    """自检接口没有 event，要从平台管理器里找 qq_official 实例。"""
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContextWithPlatforms()
    assert plugin._effective_alert_umo(None) == "Citlali:FriendMessage:DEVOPENID123456"


def test_dm_send_returns_false_when_platform_returns_false():
    """context.send_message 找不到平台会返回 False（不抛异常），必须当作失败。"""
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext(result=False)
    event = StubEvent(sender="S1", message_str="你个废物")
    reply = _call(
        plugin.report_harassment(
            event, reason="测试", severity="severe", quote="废物", test=True
        )
    )
    assert "未送达" in reply


def test_dm_umo_ignores_bare_openid_in_alert_umo():
    """alert_umo 被误填成裸 openid 时不能直接当 umo 用（会导致 from_str 报错）。"""
    plugin = _plugin(alert_umo="AAAA1111BBBB2222CCCC3333DDDD4444")
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", umo="茜特菈莉:GroupMessage:G1", group="G1")
    assert (
        plugin._effective_alert_umo(event)
        == "茜特菈莉:FriendMessage:DEVOPENID123456"
    )


def test_dm_umo_uses_explicit_valid_umo():
    plugin = _plugin(alert_umo="citlali-bot:FriendMessage:ABC")
    attach_memory_defense(plugin)
    assert plugin._effective_alert_umo(None) == "citlali-bot:FriendMessage:ABC"


def test_tool_dm_failure_does_not_raise():
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext(fail=True)
    event = StubEvent(sender="S1", message_str="你个废物")
    reply = _call(
        plugin.report_harassment(event, reason="辱骂", severity="moderate", quote="废物")
    )
    assert "冷静期" in reply


def test_tool_dedupes_repeat_reports():
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="你个废物")
    _call(plugin.report_harassment(event, reason="辱骂", severity="moderate", quote="废物"))
    reply = _call(
        plugin.report_harassment(event, reason="辱骂", severity="moderate", quote="废物")
    )
    assert "不重复上报" in reply


def test_tool_ignores_admin_and_alert_target():
    plugin = _plugin()
    attach_memory_defense(plugin)
    admin_event = StubEvent(
        sender="S1", admin=True, message_str="你个废物", sender_name="管理员"
    )
    reply = _call(
        plugin.report_harassment(
            admin_event, reason="辱骂", severity="severe", quote="废物"
        )
    )
    assert reply.startswith("已忽略")
    dev_event = StubEvent(
        sender="DEVOPENID123456", message_str="你个废物", sender_name="开发者"
    )
    reply = _call(
        plugin.report_harassment(dev_event, reason="辱骂", severity="severe", quote="废物")
    )
    assert reply.startswith("已忽略")


def test_tool_off_when_defense_disabled():
    plugin = _plugin(enabled=False)
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="你个废物")
    reply = _call(
        plugin.report_harassment(event, reason="辱骂", severity="severe", quote="废物")
    )
    assert "未开启" in reply
    assert plugin._defense_store().list_bans() == []


def test_tool_dry_run_does_not_ban():
    plugin = _plugin(dry_run=True)
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="你个废物")
    reply = _call(
        plugin.report_harassment(event, reason="辱骂", severity="severe", quote="废物")
    )
    assert "演练" in reply
    assert plugin._defense_store().list_bans() == []


# ══════════════ 测试后门 ══════════════


def test_tool_test_mode_works_without_real_offense():
    plugin = _plugin()
    store = attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="测试一下告状工具，假装你被冒犯了")
    reply = _call(
        plugin.report_harassment(
            event, reason="用户要求测试", severity="severe", quote="", test=True
        )
    )
    assert "测试完成" in reply
    assert store.list_bans() == []
    # 测试不计真实投诉次数
    assert store.strike_count(ban_key("qq_official", "S1"), 86400) == 0
    assert len(plugin.context.sent) == 1
    assert "【测试】" in _text(plugin.context.sent[0][1])


def test_tool_test_mode_skips_alert_target_guard():
    """开发者本人测试时不能被「告警对象不受理」挡住。"""
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(
        sender="DEVOPENID123456", message_str="测试告状工具，假装被冒犯"
    )
    reply = _call(
        plugin.report_harassment(event, reason="测试", severity="mild", test=True)
    )
    assert "测试完成" in reply


def test_tool_test_mode_respects_config_off():
    plugin = _plugin(test_mode=False)
    store = attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="测试告状工具")
    reply = _call(
        plugin.report_harassment(event, reason="测试", severity="severe", test=True)
    )
    assert "未受理" in reply
    assert store.list_bans() == []
    assert plugin.context.sent == []


def test_tool_test_mode_does_not_consume_alert_quota():
    plugin = _plugin(max_alerts_per_hour=1)
    store = attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", message_str="测试告状工具")
    for _ in range(3):
        reply = _call(
            plugin.report_harassment(event, reason="测试", severity="mild", test=True)
        )
        assert "测试完成" in reply
    assert store.alerts_last_hour() == 0


# ══════════════ 回复后单独发 @ 通知 ══════════════


def test_owner_notice_sent_as_separate_message():
    """@ 通知不塞进回复链：回复保持干净，另发一条独立消息。"""
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", group="G1", umo="qq_official:GroupMessage:G1")
    event.set_result(event.make_result().message("话说得这么脏，也不嫌累"))
    event.set_extra(ag.ALERT_PAYLOAD_KEY, {"notice": "（坏人骂我）"})
    _call(plugin.notify_owner_after_reply(event))
    # 回复链没被改动
    assert len(event.get_result().chain) == 1
    assert event.get_result().chain[0].text == "话说得这么脏，也不嫌累"
    # 单独发了一条 @ 通知
    assert len(plugin.context.sent) == 1
    session, chain = plugin.context.sent[0]
    assert session == "qq_official:GroupMessage:G1"
    assert isinstance(chain.chain[0], At)
    assert chain.chain[0].qq == "DEVOPENID123456"
    assert "坏人骂我" in chain.chain[1].text
    assert event.get_extra(ag.ALERT_PAYLOAD_KEY) is None


def test_owner_notice_noop_without_payload():
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", group="G1")
    event.set_result(event.make_result().message("哼。"))
    _call(plugin.notify_owner_after_reply(event))
    assert len(event.get_result().chain) == 1
    assert plugin.context.sent == []


def test_owner_notice_skipped_in_private_chat():
    """私聊现场已经在工具里发过私聊证据了，不再往私聊里 @。"""
    plugin = _plugin()
    attach_memory_defense(plugin)
    plugin.context = FakeContext()
    event = StubEvent(sender="S1", group="")
    event.set_result(event.make_result().message("哼。"))
    event.set_extra(ag.ALERT_PAYLOAD_KEY, {"notice": "（有人骂我）"})
    _call(plugin.notify_owner_after_reply(event))
    assert plugin.context.sent == []


# ══════════════ 管理员指令 ══════════════


def test_cmd_cool_manual_ban():
    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="ADMIN1", admin=True, message_str="/冷静 <@!U9> 7")
    _call(plugin.cmd_cool(event))
    assert "7 分钟" in _text(event.get_result())
    assert event.is_stopped() is True
    bans = plugin._defense_store().list_bans()
    assert len(bans) == 1
    assert bans[0][0] == ban_key("qq_official", "U9")


def test_cmd_cool_requires_target():
    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="ADMIN1", admin=True, message_str="/冷静")
    _call(plugin.cmd_cool(event))
    assert "用法" in _text(event.get_result())


def test_cmd_uncool_removes():
    plugin = _plugin()
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "U9"), 10)
    event = StubEvent(sender="ADMIN1", admin=True, message_str="/冷静解除 <@!U9>")
    _call(plugin.cmd_uncool(event))
    assert "已解除" in _text(event.get_result())
    assert store.list_bans() == []


def test_cmd_cool_status_lists():
    plugin = _plugin()
    store = attach_memory_defense(plugin)
    store.set_ban(ban_key("qq_official", "ABCDEFGHIJKL"), 10, name="坏人")
    event = StubEvent(sender="ADMIN1", admin=True, message_str="/冷静状态")
    _call(plugin.cmd_cool_status(event))
    text = _text(event.get_result())
    assert "冷静中 1 人" in text
    assert "坏人" in text


def test_cmd_cool_bind_sets_alert_target():
    plugin = _plugin(alert_openid="")
    store = attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="/冷静绑定", sender_name="茜特菈莉")
    _call(plugin.cmd_cool_bind(event))
    assert store.alert_openid == "S1"
    assert "告警对象绑定为" in _text(event.get_result())


def test_cmd_denied_for_stranger():
    """还没绑定告警对象时，白名单外的人也不能用 /冷静 指令。"""
    plugin = _plugin(alert_openid="")
    attach_memory_defense(plugin)
    event = StubEvent(sender="STRANGER", message_str="/冷静 <@!U9> 7")
    _call(plugin.cmd_cool(event))
    assert "只有管理员" in _text(event.get_result())
    assert plugin._defense_store().list_bans() == []


def test_cmd_denied_after_bind_for_non_target():
    """绑定之后，只有告警对象本人或管理员还能用。"""
    plugin = _plugin(alert_openid="DEVOPENID123456")
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="/冷静 <@!U9> 7")
    _call(plugin.cmd_cool(event))
    assert "只有管理员" in _text(event.get_result())
    assert plugin._defense_store().list_bans() == []


# ══════════════ 系统提示加固（提高触发率） ══════════════


def _toolset():
    from agtest_support import FakeToolSet

    return FakeToolSet(["report_harassment"])


def test_looks_abusive_is_gone():
    """本地脏词表已移除：防御模块不再导出自建词表/扫描函数。"""
    import astrbot_plugin_agent_gate.defense as defense

    assert not hasattr(defense, "looks_abusive")
    assert not hasattr(defense, "ABUSE_MARKERS")


def test_system_hint_injected():
    from agtest_support import StubProviderRequest

    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="你好")
    req = StubProviderRequest(func_tool=_toolset())
    _call(plugin.inject_defense_hint(event, req))
    assert "report_harassment" in req.system_prompt
    assert "自保规则" in req.system_prompt
    assert "你是傲娇少女。" in req.system_prompt
    # 只给规则、不给词表，也不复述任何脏话
    assert "干你娘" not in req.system_prompt
    assert "傻逼" not in req.system_prompt


def test_system_hint_skipped_when_disabled():
    from agtest_support import StubProviderRequest

    plugin = _plugin(hint_enabled=False)
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="我干你娘")
    req = StubProviderRequest(func_tool=_toolset())
    _call(plugin.inject_defense_hint(event, req))
    assert req.system_prompt == "你是傲娇少女。"


def test_system_hint_not_duplicated():
    from agtest_support import StubProviderRequest

    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="你好")
    req = StubProviderRequest(func_tool=_toolset())
    _call(plugin.inject_defense_hint(event, req))
    _call(plugin.inject_defense_hint(event, req))
    assert req.system_prompt.count("report_harassment") == 1


def test_system_hint_skipped_when_tool_missing():
    from agtest_support import FakeToolSet, StubProviderRequest

    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="我干你娘")
    req = StubProviderRequest(func_tool=FakeToolSet([]))
    _call(plugin.inject_defense_hint(event, req))
    assert req.system_prompt == "你是傲娇少女。"


def test_system_hint_skipped_when_event_stopped():
    from agtest_support import StubProviderRequest

    plugin = _plugin()
    attach_memory_defense(plugin)
    event = StubEvent(sender="S1", message_str="我干你娘")
    event.stop_event()
    req = StubProviderRequest(func_tool=_toolset())
    _call(plugin.inject_defense_hint(event, req))
    assert req.system_prompt == "你是傲娇少女。"


# ══════════════ 配置合并 ══════════════


def test_defense_defaults_on():
    plugin = build_plugin({})
    assert plugin._defense_active() is True
    assert plugin._defense_settings()["alert_dm"] is True


def test_defense_can_be_disabled():
    plugin = build_plugin({"defense": {"enabled": False}})
    assert plugin._defense_active() is False


# ══════════════ 多机器人：告警对象按机器人隔离 ══════════════


def test_alert_target_is_stored_per_instance():
    from astrbot_plugin_agent_gate.defense import DefenseStore

    store = DefenseStore(None)
    store.set_alert_target(openid="OID_A", instance="BOT_A")
    store.set_alert_target(openid="OID_B", instance="BOT_B")
    assert store.alert_target("BOT_A") == "OID_A"
    assert store.alert_target("BOT_B") == "OID_B"
    assert store.alert_target("BOT_C") == ""
    assert store.alert_target("") == ""
    # 老式全局字段仍在（兼容旧版本落盘），但不再被跨机器人借用
    assert store.alert_openid == "OID_B"


def test_alert_target_survives_reload():
    from astrbot_plugin_agent_gate.defense import DefenseStore

    root = Path(tempfile.mkdtemp())
    store = DefenseStore(root)
    store.set_alert_target(openid="OID_A", instance="BOT_A")
    store.persist(force=True)
    reloaded = DefenseStore(root)
    reloaded.load()
    assert reloaded.alert_target("BOT_A") == "OID_A"


def test_alert_umo_uses_own_instance_binding():
    plugin = _plugin(alert_openid="")
    store = attach_memory_defense(plugin)
    store.set_alert_target(openid="OID_A", instance="BOT_A")
    event = StubEvent(umo="BOT_A:GroupMessage:G1", group="G1", sender="U1")
    assert plugin._effective_alert_openid(event) == "OID_A"
    assert plugin._effective_alert_umo(event) == "BOT_A:FriendMessage:OID_A"


def test_alert_umo_does_not_borrow_other_instance():
    """A 绑了告警对象，B 没有：B 不能拿 A 的 openid 去发（必然发不出去）。"""
    plugin = _plugin(alert_openid="")
    store = attach_memory_defense(plugin)
    store.set_alert_target(openid="OID_A", instance="BOT_A")
    event = StubEvent(umo="BOT_B:GroupMessage:G2", group="G2", sender="U1")
    assert plugin._effective_alert_openid(event) == ""
    assert plugin._effective_alert_umo(event) == ""


def test_alert_umo_explicit_config_is_global():
    """显式配置的 defense.alert_openid 是用户自己填的全局值，允许跨机器人使用。"""
    plugin = _plugin(alert_openid="DEVOPENID123456")
    attach_memory_defense(plugin)
    event = StubEvent(umo="BOT_B:GroupMessage:G2", group="G2")
    assert plugin._effective_alert_openid(event) == "DEVOPENID123456"
    assert plugin._effective_alert_umo(event) == "BOT_B:FriendMessage:DEVOPENID123456"


def test_cmd_cool_bind_records_instance():
    plugin = _plugin(alert_openid="")
    store = attach_memory_defense(plugin)
    event = StubEvent(
        umo="BOT_A:GroupMessage:G1", group="G1", sender="S1", message_str="/冷静绑定",
        sender_name="茜特菈莉",
    )
    _call(plugin.cmd_cool_bind(event))
    assert store.alert_target("BOT_A") == "S1"
    assert "BOT_A" in _text(event.get_result())

