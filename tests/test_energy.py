# -*- coding: utf-8 -*-
"""精力账本与精力门闩（阈值 = 精力 <= 0）。"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agtest_support import (  # noqa: E402
    DictComponent,
    EnergyLedger,
    StubEvent,
    ag,
    build_plugin,
    energy_config,
    plain_chain,
    wl,
)


# ── 账本：字符数 / 扣能 / 恢复 ──


def test_effective_character_count_text_branch():
    assert EnergyLedger.effective_character_count(plain_chain("hello")) == 5


def test_effective_character_count_data_branch():
    chain = list(plain_chain("hello")) + [DictComponent("世界")]
    assert EnergyLedger.effective_character_count(chain) == 7


def test_effective_character_count_strips():
    assert EnergyLedger.effective_character_count(plain_chain("  hi  ")) == 2


def test_charge_formula():
    ledger = EnergyLedger()
    before, after, chars, cost = ledger.charge("c1", plain_chain("hello"))
    assert chars == 5
    assert abs(cost - (14.0 + 5 * 0.12)) < 1e-9
    assert abs(before - 100.0) < 1e-9
    assert abs(after - (100.0 - cost)) < 1e-9


def test_charge_empty_chain_returns_none():
    assert EnergyLedger().charge("c1", []) is None
    assert not EnergyLedger().has_state("c1")


def test_charge_clamps_at_min_energy():
    ledger = EnergyLedger(
        settings=lambda: {"base_reply_cost": 500.0, "min_energy": -10.0}
    )
    _, after, _, _ = ledger.charge("c1", plain_chain("x"))
    assert after == -10.0


def test_recover_uses_elapsed_seconds():
    ledger = EnergyLedger(
        settings=lambda: {"recovery_per_second": 1.0, "max_energy": 50.0}
    )
    state = ledger._state("c1")
    state.energy = 0.0
    state.updated_at = time.time() - 10.0
    ledger.recover("c1")
    assert 9.0 <= state.energy <= 10.5


def test_recover_caps_at_max_energy():
    ledger = EnergyLedger(
        settings=lambda: {"recovery_per_second": 100.0, "max_energy": 50.0}
    )
    state = ledger._state("c1")
    state.energy = 10.0
    state.updated_at = time.time() - 10.0
    ledger.recover("c1")
    assert state.energy == 50.0


def test_peek_is_read_only():
    ledger = EnergyLedger(settings=lambda: {"initial_energy": 42.0})
    assert ledger.peek("c1") == 42.0
    assert not ledger.has_state("c1")


def test_peek_estimates_recovery_without_mutating():
    ledger = EnergyLedger(
        settings=lambda: {"recovery_per_second": 1.0, "max_energy": 50.0}
    )
    state = ledger._state("c1")
    state.energy = 0.0
    state.updated_at = time.time() - 10.0
    assert 9.0 <= ledger.peek("c1") <= 10.5
    assert state.energy == 0.0  # 没有被改动


def test_clear_resets_all_states():
    ledger = EnergyLedger()
    ledger.get("c1")
    assert ledger.has_state("c1")
    ledger.clear()
    assert not ledger.has_state("c1")


# ── 门闩阈值 = 精力 <= 0 ──


def test_energy_zero_blocks():
    """边界：精力恰好为 0 时拦下。关掉恢复让断言可确定。"""
    plugin = build_plugin(
        {**wl("S1"), **energy_config(initial_energy=0.0, recovery_per_second=0.0)}
    )
    assert plugin._decide(StubEvent()) == "energy"


def test_energy_slightly_positive_passes():
    plugin = build_plugin({**wl("S1"), **energy_config(initial_energy=0.1)})
    assert plugin._decide(StubEvent()) == "pass"


def test_energy_negative_blocks():
    plugin = build_plugin({**wl("S1"), **energy_config(initial_energy=-5.0)})
    assert plugin._decide(StubEvent()) == "energy"


def test_min_energy_is_only_a_floor():
    """min_energy 只决定扣能地板，不影响门闩阈值。"""
    plugin = build_plugin(
        {
            **wl("S1"),
            **energy_config(
                initial_energy=1.0,
                min_energy=-100.0,
                base_reply_cost=50.0,
                reply_cost_per_character=0.0,
                recovery_per_second=0.0,
            ),
        }
    )
    event = StubEvent()
    assert plugin._decide(event) == "pass"
    plugin.energy.charge(event.unified_msg_origin, plain_chain("x"))
    assert plugin.energy.get(event.unified_msg_origin) == -49.0
    assert plugin._decide(StubEvent()) == "energy"


# ── 精力只作用于白名单内 ──


def test_stranger_never_touches_energy():
    plugin = build_plugin(wl("someone-else"))
    event = StubEvent()
    assert plugin._decide(event) == "block"
    assert plugin.energy.snapshot() == {}
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is None


def test_whitelisted_message_is_marked_eligible():
    plugin = build_plugin(wl("S1"))
    event = StubEvent()
    assert plugin._decide(event) == "pass"
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is True


def test_energy_switch_off_skips_everything():
    plugin = build_plugin(
        {**wl("S1"), **energy_config(enabled=False, initial_energy=-1.0)}
    )
    event = StubEvent()
    assert plugin._decide(event) == "pass"
    assert plugin.energy.snapshot() == {}
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is None


# ── 管理员豁免 ──


def test_admin_exempt_skips_whitelist_and_energy():
    plugin = build_plugin(
        {**wl("someone-else"), **energy_config(initial_energy=-1.0)}
    )
    event = StubEvent(admin=True)
    assert plugin._decide(event) == "pass"
    assert plugin.energy.snapshot() == {}
    assert event.extras.get(ag.ENERGY_ELIGIBLE_KEY) is None


def test_admin_without_exempt_is_treated_like_anyone():
    plugin = build_plugin(
        {
            **wl("someone-else", admin_exempt=False),
            **energy_config(initial_energy=-1.0),
        }
    )
    assert plugin._decide(StubEvent(admin=True)) == "block"


def test_admin_exempt_off_but_whitelisted_still_consumes_energy():
    plugin = build_plugin(
        {
            **wl("S1", admin_exempt=False),
            **energy_config(initial_energy=-1.0),
        }
    )
    assert plugin._decide(StubEvent(admin=True)) == "energy"


# ── 结算钩子 ──


def test_settle_energy_requires_eligible_marker():
    plugin = build_plugin({**wl("S1"), **energy_config()})
    event = StubEvent()
    event.set_result(event.plain_result("hello world"))
    asyncio.run(plugin.settle_energy(event))
    assert plugin.energy.snapshot() == {}


def test_settle_energy_charges_once_and_is_idempotent():
    plugin = build_plugin({**wl("S1"), **energy_config()})
    event = StubEvent()
    event.set_extra(ag.ENERGY_ELIGIBLE_KEY, True)
    event.set_result(event.plain_result("hello world"))

    asyncio.run(plugin.settle_energy(event))
    after_first = plugin.energy.get(event.unified_msg_origin)
    assert after_first < 100.0

    event.set_result(event.plain_result("hello again"))
    asyncio.run(plugin.settle_energy(event))
    assert plugin.energy.get(event.unified_msg_origin) == after_first


def test_settle_energy_skips_empty_chain():
    plugin = build_plugin({**wl("S1"), **energy_config()})
    event = StubEvent()
    event.set_extra(ag.ENERGY_ELIGIBLE_KEY, True)
    asyncio.run(plugin.settle_energy(event))
    assert plugin.energy.snapshot() == {}


def test_settle_energy_skips_when_gate_closed():
    plugin = build_plugin(
        {"whitelist_enabled": False, **wl("S1"), **energy_config()}
    )
    event = StubEvent()
    event.set_extra(ag.ENERGY_ELIGIBLE_KEY, True)
    event.set_result(event.plain_result("hello"))
    asyncio.run(plugin.settle_energy(event))
    assert plugin.energy.snapshot() == {}


# ── 精力耗尽时的拦截与恢复 ──


def test_energy_exhausted_replies_candidate_line():
    plugin = build_plugin(
        {**wl("S1"), **energy_config(initial_energy=-1.0, messages=["歇了"])}
    )
    event = StubEvent()
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True
    assert event.result is not None
    assert "歇了" in event.result.get_plain_text()


def test_energy_exhausted_silent_when_no_candidates():
    plugin = build_plugin(
        {**wl("S1"), **energy_config(initial_energy=-1.0, messages=[])}
    )
    event = StubEvent()
    asyncio.run(plugin.gate_waking(event))
    assert event.stopped is True
    assert event.result is None


def test_exhausted_then_recovered_passes_again():
    plugin = build_plugin({**wl("S1"), **energy_config(initial_energy=-5.0)})
    first = StubEvent()
    assert plugin._decide(first) == "energy"

    state = plugin.energy._state(first.unified_msg_origin)
    state.updated_at = time.time() - 100.0  # 100 秒 × 0.6 = 60 点
    second = StubEvent()
    assert plugin._decide(second) == "pass"
