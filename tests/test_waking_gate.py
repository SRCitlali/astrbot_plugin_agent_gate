# -*- coding: utf-8 -*-
"""唤醒阶段拦截：过滤器语义、拦指令、放行白名单、兜底钩子。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agtest_support import StubEvent, ag, build_plugin, wl  # noqa: E402


def _filter():
    """框架用 register_custom_filter(类, raise_error) 实例化过滤器。"""
    return ag.AgentGateWokenFilter(False)


# ── 过滤器：只在「门开着」且「已唤醒」时激活 ──


def test_filter_false_without_active_plugin():
    original = ag._ACTIVE_PLUGIN
    ag._ACTIVE_PLUGIN = None
    try:
        assert _filter().filter(StubEvent(wake=True), None) is False
    finally:
        ag._ACTIVE_PLUGIN = original


def test_filter_true_when_woken_and_gate_active():
    build_plugin(wl("S1"))
    assert _filter().filter(StubEvent(wake=True), None) is True


def test_filter_false_when_not_woken():
    """核心：群里没被 @ 的消息不能让处理器激活，否则会把 is_wake 置真污染框架。"""
    build_plugin(wl("S1"))
    assert _filter().filter(StubEvent(wake=False), None) is False


def test_filter_false_when_whitelist_switch_off():
    """门关了、告状也关了，才彻底不激活。"""
    build_plugin({"whitelist_enabled": False, "defense": {"enabled": False}})
    assert _filter().filter(StubEvent(wake=True), None) is False


def test_filter_true_when_only_defense_on():
    """冷静期独立于白名单门：门关了也要能拦正在冷静期的人。"""
    build_plugin({"whitelist_enabled": False, "defense": {"enabled": True}})
    assert _filter().filter(StubEvent(wake=True), None) is True


def test_filter_true_when_only_energy_switch_off():
    """精力开关关掉不影响白名单拦截。"""
    build_plugin({"energy_enabled": False, **wl("S1")})
    assert _filter().filter(StubEvent(wake=True), None) is True


def test_filter_never_raises_on_broken_event():
    build_plugin(wl("S1"))

    class Broken:
        @property
        def is_at_or_wake_command(self):
            raise RuntimeError("boom")

    assert _filter().filter(Broken(), None) is False


# ── 处理器注册 ──


def test_waking_handler_registered_on_adapter_message_event():
    from astrbot.core.star.star_handler import (  # noqa: PLC0415
        EventType,
        star_handlers_registry,
    )

    handlers = star_handlers_registry.get_handlers_by_module_name(ag.__name__)
    matched = [
        h for h in handlers if getattr(h.handler, "__name__", "") == "gate_waking"
    ]
    assert matched, "gate_waking 没有注册进处理器注册表"
    handler = matched[0]
    assert handler.event_type == EventType.AdapterMessageEvent
    assert handler.extras_configs.get("priority") == ag.GATE_WAKING_PRIORITY
    assert any(
        isinstance(f, ag.AgentGateWokenFilter) for f in handler.event_filters
    )


# ── 唤醒阶段拦截行为 ──


def test_gate_waking_blocks_stranger_with_id_in_reply():
    plugin = build_plugin(wl("someone-else", recovery_messages=["ID={sender_id}"]))
    event = StubEvent(sender="ZZZ", wake=True)
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True
    assert event.result is not None
    assert "ID=ZZZ" in event.result.get_plain_text()


def test_gate_waking_blocks_command_from_stranger():
    """需求核心：名单外的指令也要一并拦下。"""
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent(sender="ZZZ", wake=True, message_str="/占卜 情感")
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True
    assert event.result is not None


def test_gate_waking_blocks_builtin_command_from_stranger():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent(sender="ZZZ", wake=True, message_str="/help")
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True


def test_gate_waking_silent_when_recovery_messages_empty():
    plugin = build_plugin(wl("someone-else", recovery_messages=[]))
    event = StubEvent(sender="ZZZ", wake=True)
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True
    assert event.result is None


def test_gate_waking_passes_whitelisted_and_marks_eligible():
    plugin = build_plugin(wl("S1"))
    event = StubEvent()
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is False
    assert event.result is None
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is True


def test_gate_waking_passes_whitelisted_command():
    plugin = build_plugin(wl("S1"))
    event = StubEvent(message_str="/占卜 情感")
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is False


def test_gate_waking_noop_when_whitelist_switch_off():
    plugin = build_plugin({"whitelist_enabled": False})
    event = StubEvent(sender="ZZZ", wake=True, message_str="/占卜")
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is False
    assert event.result is None


def test_gate_waking_admin_exempt_passes():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent(sender="ZZZ", admin=True)
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is False


# ── LLM 兜底钩子 ──


def test_gate_early_blocks_stranger():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent(sender="ZZZ")
    asyncio.run(plugin.gate_early(event))
    assert event.stopped is True
    assert event.result is not None


def test_gate_llm_hook_blocks_stranger():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent(sender="ZZZ")
    asyncio.run(plugin.gate(event, object()))
    assert event.stopped is True
    assert event.result is not None


def test_llm_hooks_pass_whitelisted():
    plugin = build_plugin(wl("S1"))
    event = StubEvent()
    asyncio.run(plugin.gate_early(event))
    assert event.stopped is False
    event2 = StubEvent()
    asyncio.run(plugin.gate(event2, object()))
    assert event2.stopped is False
