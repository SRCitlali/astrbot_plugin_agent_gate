# -*- coding: utf-8 -*-
"""白名单判定、旧配置迁移、开关、拦截文案。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agtest_support import StubEvent, ag, build_plugin, wl  # noqa: E402


def _m(**kw):
    """调用纯函数 _match_whitelist，未指定的维度给空值。"""
    base = dict(
        platform="qq_official",
        instance="",
        sender="S1",
        session="ABC",
        umo="qq_official:FriendMessage:ABC",
        group="",
        whitelist=set(),
    )
    base.update(kw)
    return ag._match_whitelist(**base)


# ── 单表：一张表管三种身份 ──


def test_empty_whitelist_blocks():
    assert _m() is False


def test_sender_hits():
    assert _m(whitelist={"S1"}) is True


def test_sender_hits_in_any_group():
    """白名单用户在非白名单群里也要能唤醒。"""
    assert _m(group="G999", whitelist={"S1"}) is True


def test_group_hits():
    assert _m(group="G1", whitelist={"G1"}) is True


def test_session_hits():
    assert _m(session="ABC", whitelist={"ABC"}) is True


def test_full_umo_hits():
    assert _m(whitelist={"qq_official:FriendMessage:ABC"}) is True


def test_private_chat_user_id_is_session_id():
    """私聊里用户 ID 就是会话 ID，只填一个也能同时命中两个维度。"""
    event = StubEvent(sender="U9", session="U9", umo="qq_official:FriendMessage:U9")
    assert build_plugin(wl("U9"))._is_whitelisted(event) is True


def test_platform_prefixed_hits():
    assert _m(whitelist={"qq_official:S1"}) is True


def test_platform_prefixed_group_hits():
    assert _m(group="G1", whitelist={"qq_official:G1"}) is True


def test_other_platform_does_not_hit():
    assert _m(platform="telegram", whitelist={"qq_official:S1"}) is False


def test_unrelated_entry_does_not_hit():
    assert _m(whitelist={"someone-else"}) is False


# ── 集成：走真实 event 取值路径 ──


def test_is_whitelisted_by_sender():
    assert build_plugin(wl("S1"))._is_whitelisted(StubEvent()) is True


def test_is_whitelisted_by_full_umo():
    plugin = build_plugin(wl("qq_official:FriendMessage:ABC"))
    assert plugin._is_whitelisted(StubEvent()) is True


def test_whitelist_entries_are_stripped_and_deduped():
    plugin = build_plugin(wl("  S1  ", None, "  ", "S1", "G1"))
    assert plugin._whitelist_entries() == {"S1", "G1"}


def test_to_set_drops_none_and_blank():
    assert ag._to_set([None, "", "  ", "x"]) == {"x"}


# ── 旧配置迁移 ──


def test_v2_flat_whitelist_list_still_works():
    """v2.0.0 的顶层列表写法仍然认。"""
    assert build_plugin({"whitelist": ["S1"]})._whitelist_entries() == {"S1"}


def test_legacy_three_tables_fold_into_entries():
    plugin = build_plugin(
        {
            "whitelist_users": ["U1"],
            "whitelist_groups": ["G1"],
            "whitelist_sessions": ["S1"],
        }
    )
    assert plugin._whitelist_entries() == {"U1", "G1", "S1"}


def test_legacy_fold_keeps_new_entries_and_dedupes():
    plugin = build_plugin(
        {
            "whitelist": {"entries": ["U1", "N1"]},
            "whitelist_users": ["U1"],
            "whitelist_sessions": ["S1"],
        }
    )
    assert plugin._whitelist_entries() == {"U1", "N1", "S1"}


def test_legacy_flat_settings_migrate_into_whitelist_group():
    plugin = build_plugin(
        {
            "admin_exempt": False,
            "block_notify": "silent",
            "block_cooldown_seconds": 7,
            "recovery_message": "旧文案 {sender_id}",
        }
    )
    cfg = plugin._whitelist_cfg()
    assert cfg["admin_exempt"] is False
    assert plugin._notify_mode() == "silent"
    assert plugin._cooldown_seconds() == 7
    assert plugin._recovery_messages() == ["旧文案 {sender_id}"]


def test_legacy_v2_energy_switch_migrates():
    plugin = build_plugin({"energy": {"enabled": False}})
    assert plugin._energy_switch_on() is False


def test_legacy_fold_count_recorded():
    plugin = build_plugin({"whitelist_users": ["U1", "U2"]})
    assert plugin.config["_legacy_whitelist_count"] == 2


# ── 开关 ──


def test_gate_active_by_default():
    assert build_plugin({})._gate_active() is True


def test_whitelist_switch_off_closes_gate():
    plugin = build_plugin({"whitelist_enabled": False, **wl("S1")})
    assert plugin._gate_active() is False
    assert plugin._decide(StubEvent(sender="stranger")) == "pass"


def test_energy_switch_off_keeps_whitelist_check():
    plugin = build_plugin({"energy_enabled": False, **wl("S1")})
    assert plugin._gate_active() is True
    assert plugin._decide(StubEvent(sender="stranger")) == "block"
    assert plugin._decide(StubEvent(sender="S1")) == "pass"


def test_switch_off_means_no_energy_state():
    plugin = build_plugin({"whitelist_enabled": False, **wl("S1")})
    event = StubEvent()
    assert plugin._decide(event) == "pass"
    assert plugin.energy.snapshot() == {}
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is None


# ── 判定结果 ──


def test_decide_blocks_stranger():
    assert build_plugin(wl("someone-else"))._decide(StubEvent()) == "block"


def test_decide_passes_whitelisted():
    assert build_plugin(wl("S1"))._decide(StubEvent()) == "pass"


def test_decide_marks_eligible_for_whitelisted():
    plugin = build_plugin(wl("S1"))
    event = StubEvent()
    assert plugin._decide(event) == "pass"
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is True


def test_is_allowed_is_read_only():
    plugin = build_plugin(wl("S1"))
    event = StubEvent()
    assert plugin.is_allowed(event) is True
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is None
    assert plugin.energy.snapshot() == {}


def test_is_allowed_false_for_stranger():
    assert build_plugin(wl("someone-else")).is_allowed(StubEvent()) is False


# ── 拦截文案 ──


def test_recovery_messages_single_line_render():
    plugin = build_plugin(wl("x", recovery_messages=["ID={sender_id}"]))
    assert plugin._pick_recovery(StubEvent()) == "ID=S1"


def test_recovery_messages_randomly_pick_from_all():
    plugin = build_plugin(wl("x", recovery_messages=["甲", "乙", "丙"]))
    seen = {plugin._pick_recovery() for _ in range(200)}
    assert seen == {"甲", "乙", "丙"}


def test_recovery_messages_empty_means_no_reply():
    plugin = build_plugin(wl("x", recovery_messages=[]))
    assert plugin._pick_recovery(StubEvent()) == ""


def test_recovery_messages_blank_lines_dropped():
    plugin = build_plugin(wl("x", recovery_messages=["  ", "有内容", None]))
    assert plugin._recovery_messages() == ["有内容"]


def test_recovery_message_unknown_placeholder_kept():
    plugin = build_plugin(wl("x", recovery_messages=["x={nope}"]))
    assert plugin._pick_recovery(StubEvent()) == "x={nope}"


def test_recovery_message_broken_template_falls_back():
    plugin = build_plugin(wl("x", recovery_messages=["落单的 { 大括号"]))
    assert plugin._pick_recovery(StubEvent()) == "落单的 { 大括号"


def test_recovery_message_full_umo_placeholder():
    plugin = build_plugin(wl("x", recovery_messages=["umo={unified_msg_origin}"]))
    assert plugin._pick_recovery(StubEvent()) == "umo=qq_official:FriendMessage:ABC"


# ── 非法配置 ──


def test_invalid_notify_falls_back_to_on_mention():
    plugin = build_plugin(wl("x", block_notify="nonsense"))
    assert plugin._notify_mode() == "on_mention"


def test_invalid_cooldown_falls_back_to_zero():
    plugin = build_plugin(wl("x", block_cooldown_seconds="abc"))
    assert plugin._cooldown_seconds() == 0


def test_energy_group_missing_is_filled_with_defaults():
    plugin = build_plugin({})
    assert plugin._energy_cfg()["initial_energy"] == 100.0
    assert plugin._energy_switch_on() is True


def test_whitelist_group_missing_is_filled_with_defaults():
    plugin = build_plugin({})
    assert plugin._whitelist_cfg()["block_notify"] == "on_mention"
    assert plugin._whitelist_cfg()["admin_exempt"] is True
    assert len(plugin._recovery_messages()) == 3


# ── 多机器人：按机器人限定的白名单写法 ──


def test_instance_prefixed_entry_only_for_that_bot():
    assert _m(instance="BOT_A", whitelist={"BOT_A:S1"}) is True
    assert _m(instance="BOT_B", whitelist={"BOT_A:S1"}) is False


def test_adapter_prefixed_entry_still_works():
    """旧写法（适配器类型前缀）继续可用，别把老配置弄失效。"""
    assert _m(instance="BOT_B", whitelist={"qq_official:S1"}) is True


def test_bare_entry_works_on_every_bot():
    assert _m(instance="BOT_A", whitelist={"S1"}) is True
    assert _m(instance="BOT_B", whitelist={"S1"}) is True

