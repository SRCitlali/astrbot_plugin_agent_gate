# -*- coding: utf-8 -*-
"""拦截幂等、回话策略与冷却、启动自检、终止清理。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agtest_support import StubEvent, ag, build_plugin, wl  # noqa: E402


class _LogRecorder:
    """替换 ag.logger，记录 (level, message)。"""

    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def _record(message, *args, **kwargs):
            self.records.append((level, str(message)))

        return _record

    def levels(self):
        return [level for level, _ in self.records]


class _StubContext:
    def __init__(self, config):
        self._config = config

    def get_config(self, *args, **kwargs):
        return self._config


def _capture(func):
    """在替换 logger 的前提下执行 func，返回记录器。"""
    original = ag.logger
    recorder = _LogRecorder()
    ag.logger = recorder
    try:
        func()
    finally:
        ag.logger = original
    return recorder


# ── 幂等 ──


def test_block_replies_once_for_same_event():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent()

    asyncio.run(plugin.gate_waking(event))
    assert event.result is not None

    event.result = None
    asyncio.run(plugin.gate_early(event))
    assert event.result is None, "同一事件不应被重复回复"


def test_blocked_marker_is_written():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent()
    asyncio.run(plugin.gate_waking(event))
    assert event.extras.get(ag.BLOCKED_KEY) is True


def test_waking_and_llm_fallback_do_not_double_reply():
    plugin = build_plugin(wl("someone-else", block_cooldown_seconds=0))
    event = StubEvent()
    asyncio.run(plugin.gate_waking(event))
    first_text = event.result.get_plain_text()
    event.result = None
    asyncio.run(plugin.gate(event, object()))
    assert event.result is None
    assert first_text


# ── 回话策略 ──


def test_silent_mode_does_not_reply():
    plugin = build_plugin(wl("someone-else", block_notify="silent"))
    event = StubEvent()
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True
    assert event.result is None


def test_on_mention_silent_when_not_woken():
    plugin = build_plugin(wl("someone-else", block_notify="on_mention"))
    event = StubEvent(wake=False)
    asyncio.run(plugin.gate_waking(event))
    assert event.result is None


def test_always_replies_even_when_not_woken():
    plugin = build_plugin(wl("someone-else", block_notify="always"))
    event = StubEvent(wake=False)
    asyncio.run(plugin.gate_waking(event))
    assert event.result is not None
    assert event.result.get_plain_text()


def test_reply_text_comes_from_recovery_messages():
    plugin = build_plugin(
        wl(
            "someone-else",
            recovery_messages=["甲", "乙"],
            block_notify="always",
            block_cooldown_seconds=0,
        )
    )
    seen = set()
    for _ in range(60):
        event = StubEvent(wake=True)
        asyncio.run(plugin.gate_waking(event))
        seen.add(event.result.get_plain_text())
    assert seen <= {"甲", "乙"}
    assert len(seen) == 2, "应当从候选里随机挑"


# ── 冷却 ──


def test_cooldown_blocks_second_reply():
    plugin = build_plugin(wl("someone-else", block_cooldown_seconds=60))
    first = StubEvent()
    asyncio.run(plugin.gate_waking(first))
    assert first.result is not None

    second = StubEvent()
    asyncio.run(plugin.gate_waking(second))
    assert second.stopped is True
    assert second.result is None


def test_cooldown_query_does_not_write_state():
    plugin = build_plugin(wl("x", block_cooldown_seconds=60))
    event = StubEvent()
    assert plugin._is_cooling_down(event) is False
    assert plugin._last_notify == {}
    plugin._mark_notified(event)
    assert event.unified_msg_origin in plugin._last_notify
    assert plugin._is_cooling_down(event) is True


def test_cooldown_table_is_bounded():
    plugin = build_plugin(wl("x", block_cooldown_seconds=1))
    for index in range(ag.MAX_COOLDOWN_ENTRIES + 50):
        plugin._mark_notified(StubEvent(umo=f"umo:{index}"))
    assert len(plugin._last_notify) <= ag.MAX_COOLDOWN_ENTRIES


# ── 启动自检 ──


def test_plugin_set_star_is_ok():
    def _run():
        plugin = build_plugin({})
        plugin.context = _StubContext({"plugin_set": ["*"]})
        plugin._check_plugin_set()

    recorder = _capture(_run)
    assert "error" not in recorder.levels()
    assert "info" in recorder.levels()


def test_plugin_set_narrowed_without_us_is_an_error():
    def _run():
        plugin = build_plugin({})
        plugin.context = _StubContext({"plugin_set": ["other_plugin"]})
        plugin._check_plugin_set()

    recorder = _capture(_run)
    assert "error" in recorder.levels(), "收窄 plugin_set 必须报 ERROR"


def test_plugin_set_narrowed_but_contains_us_is_ok():
    def _run():
        plugin = build_plugin({})
        plugin.context = _StubContext({"plugin_set": [ag.PLUGIN_DIR_NAME]})
        plugin._check_plugin_set()

    recorder = _capture(_run)
    assert "error" not in recorder.levels()


def test_plugin_set_missing_does_not_crash():
    plugin = build_plugin({})
    plugin.context = _StubContext({})
    plugin._check_plugin_set()


def test_empty_whitelist_warns():
    def _run():
        plugin = build_plugin({})
        plugin.context = _StubContext({"plugin_set": ["*"], "admins_id": ["me"]})
        plugin._log_startup_summary()

    recorder = _capture(_run)
    assert "warning" in recorder.levels()


def test_whitelist_switch_off_logged_in_summary():
    def _run():
        plugin = build_plugin({"whitelist_enabled": False})
        plugin.context = _StubContext({"plugin_set": ["*"]})
        plugin._log_startup_summary()

    recorder = _capture(_run)
    messages = " ".join(msg for _, msg in recorder.records)
    assert "白名单开关为关闭状态" in messages


def test_summary_reports_both_switches():
    def _run():
        plugin = build_plugin({"energy_enabled": False, **wl("S1")})
        plugin.context = _StubContext({"plugin_set": ["*"], "admins_id": ["me"]})
        plugin._log_startup_summary()

    recorder = _capture(_run)
    messages = " ".join(msg for _, msg in recorder.records)
    assert "精力开关为关闭状态" in messages


# ── 终止清理 ──


def test_terminate_clears_state():
    plugin = build_plugin(wl("S1", block_cooldown_seconds=60))
    event = StubEvent()
    plugin._mark_notified(event)
    plugin.energy.get(event.unified_msg_origin)
    assert plugin._last_notify
    assert plugin.energy.has_state(event.unified_msg_origin)

    asyncio.run(plugin.terminate())
    assert plugin._last_notify == {}
    assert plugin.energy.snapshot() == {}
    assert ag._ACTIVE_PLUGIN is None
