# -*- coding: utf-8 -*-
"""AgentGate 会话总览（插件页面）的自检脚本。

只测新增的 `gate_sessions` 模块和 `main.py` 的接线，不碰 AgentGate 原有的守门逻辑。

跑法：python tests/test_gate_page.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PASSED = 0
FAILED: list[str] = []


def check(desc, ok, detail=""):
    global PASSED
    if ok:
        PASSED += 1
    else:
        FAILED.append(f"{desc}  {detail}")


def eq(desc, actual, expected):
    check(desc, actual == expected, f"期望 {expected!r}，实际 {actual!r}")


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    star = types.ModuleType("astrbot.api.star")

    class StarTools:
        _dir = Path(tempfile.mkdtemp())

        @staticmethod
        def get_data_dir(name):
            p = StarTools._dir / name
            p.mkdir(parents=True, exist_ok=True)
            return str(p)

    star.StarTools = StarTools
    api.star = star
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star


def main() -> int:
    _install_stubs()
    sys.path.insert(0, str(PLUGIN_DIR.parent))

    pkg = types.ModuleType("astrbot_plugin_agent_gate")
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules["astrbot_plugin_agent_gate"] = pkg

    mod = importlib.import_module("astrbot_plugin_agent_gate.gate_sessions")

    # ── 1. 相对时间 ──
    eq("刚刚", mod._ago_text(time.time()).endswith("秒前"), True)
    check("3 分钟前", mod._ago_text(time.time() - 200).endswith("分钟前"))
    check("5 小时前", mod._ago_text(time.time() - 5 * 3600).endswith("小时前"))
    check("2 天前", mod._ago_text(time.time() - 2 * 86400).endswith("天前"))
    eq("没记录返回 —", mod._ago_text(0), "—")
    eq("坏值返回 —", mod._ago_text("abc"), "—")
    eq("None 返回 —", mod._ago_text(None), "—")

    # ── 2. umo 拆分 ──
    eq("正常 umo", mod._parse_umo("茜特菈莉:GroupMessage:ABC"), ("茜特菈莉", "GroupMessage", "ABC"))
    eq("会话 ID 里带冒号也不切坏", mod._parse_umo("P:FriendMessage:A:B"), ("P", "FriendMessage", "A:B"))
    eq("残缺 umo 不炸", mod._parse_umo("X"), ("X", "", ""))

    # ── 3. 假事件 + 假插件 ──
    class FakeEvent:
        def __init__(self, umo, group_id="", sender_id="", sender_name="", platform="qq_official"):
            self.unified_msg_origin = umo
            self._group = group_id
            self._sid = sender_id
            self._sname = sender_name
            self._platform = platform

        def get_platform_name(self):
            return self._platform

        def get_group_id(self):
            return self._group

        def get_sender_id(self):
            return self._sid

        def get_sender_name(self):
            return self._sname

    class FakeAdapter:
        def __init__(self, name, pid):
            self._name = name
            self._id = pid
            self.client = types.SimpleNamespace(http=object())

        def meta(self):
            return types.SimpleNamespace(name=self._name, id=self._id)

    class FakeContext:
        def __init__(self):
            self.platform_manager = types.SimpleNamespace(
                platform_insts=[FakeAdapter("qq_official", "茜特菈莉")]
            )

        def get_platform_inst(self, pid):
            for inst in self.platform_manager.platform_insts:
                if inst.meta().id == pid:
                    return inst
            return None

        def get_db(self):
            raise RuntimeError("测试环境没有数据库")

    class FakePlugin:
        PLUGIN_DIR_NAME = "astrbot_plugin_agent_gate"

        def __init__(self, ids=None):
            self.context = FakeContext()
            self._ids = list(ids or [])

        def whitelist_ids(self):
            return sorted(self._ids)

    # ── 4. 记录会话 ──
    book = mod.SessionBook(FakePlugin(["E02CC8928D8D1437F4962C32B91D4ACA"]), PLUGIN_DIR)

    book.remember(FakeEvent("茜特菈莉:GroupMessage:G1", "G1", "U1", "小明"))
    book.remember(FakeEvent("茜特菈莉:GroupMessage:G1", "G1", "U2", "小红"))
    book.remember(FakeEvent("茜特菈莉:FriendMessage:U1", "", "U1", ""))
    eq("记到 2 个会话", len(book.sessions), 2)
    eq("群消息计数", book.sessions["茜特菈莉:GroupMessage:G1"]["msg_count"], 2)
    eq("私聊消息计数", book.sessions["茜特菈莉:FriendMessage:U1"]["msg_count"], 1)
    eq("群会话带 group_openid", book.sessions["茜特菈莉:GroupMessage:G1"]["group_openid"], "G1")
    eq("私聊会话 user_openid 就是 session_id", book.sessions["茜特菈莉:FriendMessage:U1"]["user_openid"], "U1")
    eq("攒到昵称表", book.openid_names.get("U1"), "小明")
    eq("私聊自己不带昵称", book.sessions["茜特菈莉:FriendMessage:U1"]["sender_name"], "")

    # 非 QQ 平台的消息要丢掉
    book.remember(FakeEvent("webchat:FriendMessage:abc", platform="webchat"))
    eq("非 qq_official 平台不记录", len(book.sessions), 2)

    # ── 5. 落盘 / 读回 ──
    book.persist(force=True)
    book2 = mod.SessionBook(FakePlugin(["G1"]), PLUGIN_DIR)
    book2.load()
    eq("读回 2 个会话", len(book2.sessions), 2)
    eq("读回昵称表", book2.openid_names.get("U1"), "小明")

    # ── 6. 组装 payload ──
    async def _no_meta(platform_id, group_openid):
        return {"info": {}, "state": {}, "info_error": "", "state_error": ""}

    book2.group_meta = _no_meta
    payload = asyncio.run(book2.build_payload())
    rows = {r["openid"]: r for r in payload["sessions"]}
    eq("payload 有 2 行", len(payload["sessions"]), 2)
    eq("群在前", payload["sessions"][0]["kind"], "group")
    eq("群标记已白名单", rows["G1"]["whitelisted"], True)
    eq("私聊未白名单", rows["U1"]["whitelisted"], False)
    eq("私聊借到昵称", rows["U1"]["name"], "小明")
    eq("借名有标注", rows["U1"]["name_hint"], "群里见过的昵称")
    eq("私聊默认名兜底", rows["U1"]["name"] if not rows["U1"]["name_hint"] else "小明", "小明")
    check("payload 有 summary", isinstance(payload.get("summary"), dict))
    check("payload 有 whitelist", isinstance(payload.get("whitelist"), list))
    check("payload 有 generated_at", isinstance(payload.get("generated_at"), int))
    eq("私聊的角色列是 —", rows["U1"]["role_text"], "—")
    eq("私聊的接收设置列是 —", rows["U1"]["recv_text"], "—")

    # 白名单里但没登记过的 ID 也要出现（否则删不掉）
    orphan = mod.SessionBook(FakePlugin(["NEVER_SEEN_ID"]), PLUGIN_DIR)
    orphan._loaded = True      # 别去读上面那本共享的登记表
    orphan._db_seeded = True
    orphan.group_meta = _no_meta
    op = asyncio.run(orphan.build_payload())
    eq("孤儿白名单 ID 也会列出", len(op["sessions"]), 1)
    eq("孤儿标记为未登记", op["sessions"][0]["kind"], "unknown")
    eq("孤儿是白名单状态", op["sessions"][0]["whitelisted"], True)

    # 落盘路径必须能在 AgentGate 的类上取不到 PLUGIN_DIR_NAME 时兜底，
    # 否则登记表永远不落盘、每次重启丢数据。
    eq("模块里有 PLUGIN_DIR_NAME 兜底", mod.PLUGIN_DIR_NAME, "astrbot_plugin_agent_gate")

    class _NoDirName:
        def __init__(self):
            self.context = FakeContext()

        def whitelist_ids(self):
            return []

    book4 = mod.SessionBook(_NoDirName(), PLUGIN_DIR)
    book4.sessions = {"x:GroupMessage:y": {"umo": "x:GroupMessage:y"}}
    eq("类上没常量时 _store_path 也不返回 None", book4._store_path() is not None, True)

    # ── 7. data.js 写出 ──
    import shutil

    tmp_plugin = Path(tempfile.mkdtemp())
    (tmp_plugin / "pages" / "whitelist").mkdir(parents=True)
    book3 = mod.SessionBook(FakePlugin(["G1"]), tmp_plugin)
    book3.sessions = dict(book.sessions)
    book3.openid_names = dict(book.openid_names)
    async def _no_meta3(platform_id, group_openid):
        return {"info": {"group_name": "测试群", "group_member_num": 42},
                "state": {"recv_msg_setting": "all", "member_role": "admin",
                          "allow_proactive_msg": True},
                "info_error": "", "state_error": ""}
    book3.group_meta = _no_meta3
    book3._db_seeded = True
    book3._loaded = True
    asyncio.run(book3.write_data_js())
    out = tmp_plugin / "pages" / "whitelist" / "data.js"
    check("data.js 写出来了", out.is_file(), str(out))
    if out.is_file():
        body = out.read_text(encoding="utf-8")
        check("data.js 给 __GATE_PAYLOAD__ 赋值", "__GATE_PAYLOAD__ = " in body)
        check("data.js 以分号结尾", body.strip().endswith(";"))
        parsed = json.loads(body.split(" = ", 1)[1].rstrip().rstrip(";"))
        eq("data.js 里群名正确", parsed["sessions"][0]["name"], "测试群")
        eq("data.js 里成员数正确", parsed["sessions"][0]["member_count"], 42)
        eq("data.js 里角色中文正确", parsed["sessions"][0]["role_text"], "管理员")
        eq("data.js 里接收设置中文正确", parsed["sessions"][0]["recv_text"], "全量消息")
        eq("data.js 里主动推送正确", parsed["sessions"][0]["proactive_text"], "已开启")
    shutil.rmtree(tmp_plugin, ignore_errors=True)

    # ── 8. main.py 接线（静态检查，避免为了 import 去 stub 一大堆框架 API）──
    main_src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("main.py 导入了 SessionBook", "from .gate_sessions import SessionBook" in main_src)
    check("main.py 记录了原始 config", "self.raw_config = config" in main_src)
    check("main.py 建了 sessions", "self.sessions = SessionBook(" in main_src)
    check("main.py 注册了页面写接口", "_api_whitelist" in main_src and "register_web_api" in main_src)
    check("写接口用插件目录名做前缀", 'f"/{PLUGIN_DIR_NAME}/whitelist"' in main_src)
    check("写接口成功也打日志", "AgentGate 白名单接口: action=" in main_src)
    check("写完同步刷新 data.js（不等后台任务）", "await self.sessions.write_data_js()" in main_src)
    check("写失败/异常也会记日志", "AgentGate 白名单接口异常" in main_src)
    check("main.py 有会话记录处理器", "async def record_session" in main_src)
    check("记录处理器是 ALL 类型", "EventMessageType.ALL, priority=1000" in main_src)
    check("记录处理器不打断事件", ".stop_event(" not in main_src.split("async def record_session")[1].split("async def ")[0])
    check("main.py 卸载时收掉后台任务", "self.sessions.stop()" in main_src)
    check("白名单写会重置缓存签名", "self._wl_sig = None" in main_src)
    check("白名单写会调 save_config", "save_config" in main_src)

    # ── 9. 页面静态检查 ──
    page = PLUGIN_DIR / "pages" / "whitelist" / "index.html"
    check("页面存在", page.is_file(), str(page))
    if page.is_file():
        html = page.read_text(encoding="utf-8")
        check("静态引入 ./data.js", '<script src="./data.js"></script>' in html)
        check("读的是 __GATE_PAYLOAD__", "__GATE_PAYLOAD__" in html)
        check("按钮文案是「从白名单中移除」", "从白名单中移除" in html)
        check("也有「加入白名单」", "加入白名单" in html)
        check("加载带 asset_token", "asset_token" in html and "DIAG.token" in html)
        check("动态加载防缓存", 'url.searchParams.set("t", String(Date.now()))' in html)
        check("写操作主通道是 fetch+Bearer", "function fetchWrite" in html)
        check("写了 Authorization: Bearer", '"Bearer " + DIAG.token' in html)
        check("bridge 只做兜底", "function bridgeWrite" in html and "改用 bridge" in html)
        check("写前先 await bridge.ready()（兜底通道）", "b.ready ? b.ready()" in html)
        check("写后轮询核对而不是死等一次", "function pollForChange" in html)
        check("核对最长等 12 秒（save_config 会重载插件）", "pollForChange(action, id, 12000)" in html)
        check("令牌过期有专门提示", "asset_token 过期" in html or "安全令牌已过期" in html)
        check("两条通道都失败会显示各自原因", "bridge 也失败" in html or "两条写入通道都报错" in html)
        check("诊断面板显示写通道状态", "bridge 写操作" in html and "bridge 对象" in html)
        check("没有残留的 allowed_groups 提示", "allowed_groups" not in html)
        check("失败只挂横幅不盖表格", "function showBanner" in html)
        check("页面标题是 AgentGate", "AgentGate · 会话与白名单" in html)
        check("没有残留的群管理字样", "QQ 官方群管理" not in html)
        for col in ("消息数", "最后活跃", "机器人接收消息", "机器人角色", "主动推送", "群成员数", "会话 openID"):
            check(f"表头含「{col}」", col in html)
        check("表头含「机器人」列", "<th>机器人</th>" in html)
        check("有机器人筛选容器", 'id="botFilterBox"' in html)
        check("筛选 chips 可点", "data-pid" in html and "botFilter" in html)
        check("错误分级用 error_kind", "error_kind" in html)
        check("未启用的机器人有标记", "未启用" in html)
        check("图例说明 openID 按机器人隔离", "同一个群被两个机器人加入时" in html)

    # ── 10. 多机器人：配对、错误分类、payload ──
    eq("qq_official 是 QQ", mod.is_qq_adapter("qq_official"), True)
    eq("webhook 也是 QQ", mod.is_qq_adapter("qq_official_webhook"), True)
    eq("webchat 不是", mod.is_qq_adapter("webchat"), False)
    eq("空值不是", mod.is_qq_adapter(None), False)

    for raw, expect in [
        ("鉴权失败，机器人非群成员", "not_in_group"),
        ("invalid access_token", "bot_auth"),
        ("HTTP 401 unauthorized", "bot_auth"),
        ("未找到 botpy，无法调用 QQ 官方接口", "bot_unavailable"),
        ("something odd", "unknown"),
        ("", ""),
    ]:
        eq("错误分类 " + (raw[:12] or "空"), mod.classify_meta_error(raw), expect)
    check("分类文案是人话", "不在该群" in mod.meta_error_text("not_in_group", "BOT_A"))

    class FakeContext2(FakeContext):
        def __init__(self):
            self.platform_manager = types.SimpleNamespace(
                platform_insts=[
                    FakeAdapter("qq_official", "BOT_A"),
                    FakeAdapter("qq_official_webhook", "BOT_B"),
                ]
            )

    class FakePlugin2(FakePlugin):
        def __init__(self, ids=None):
            self.context = FakeContext2()
            self._ids = list(ids or [])

    book5 = mod.SessionBook(FakePlugin2(["G1", "NEVER"]), PLUGIN_DIR)
    book5.remember(FakeEvent("BOT_A:GroupMessage:G1", "G1", "U1", "小明"))
    book5.remember(FakeEvent("BOT_B:GroupMessage:G2", "G2", "U1", "小明"))
    eq("两个机器人各记一个会话", len(book5.sessions), 2)
    eq("A 会话归属 A", book5.sessions["BOT_A:GroupMessage:G1"]["platform_id"], "BOT_A")
    eq("B 会话归属 B", book5.sessions["BOT_B:GroupMessage:G2"]["platform_id"], "BOT_B")
    eq("注册表认出两个机器人", sorted(book5.bot_registry), ["BOT_A", "BOT_B"])

    calls = []

    async def _meta_spy(platform_id, group_openid):
        calls.append((platform_id, group_openid))
        return {
            "info": {"group_name": platform_id + "的群", "group_member_num": 3},
            "state": {},
            "info_error": "",
            "state_error": "",
        }

    book5.group_meta = _meta_spy
    book5._loaded = True
    book5._db_seeded = True
    payload5 = asyncio.run(book5.build_payload())
    eq("每行只用自己的机器人查群信息", set(calls), {("BOT_A", "G1"), ("BOT_B", "G2")})
    eq("payload 有 bots 段", len(payload5["bots"]), 2)
    eq("summary 统计机器人数", payload5["summary"]["bots"], 2)
    check("每行都带 bot_label", all(r["bot_label"] for r in payload5["sessions"]))
    check("每行都带 bot_enabled", all("bot_enabled" in r for r in payload5["sessions"]))
    eq(
        "群名按各自机器人读",
        {r["openid"]: r["name"] for r in payload5["sessions"] if r["kind"] == "group"},
        {"G1": "BOT_A的群", "G2": "BOT_B的群"},
    )
    eq(
        "白名单条目带归属",
        [d["bot_label"] for d in payload5["whitelist_detail"] if d["id"] == "G1"],
        ["BOT_A"],
    )
    eq(
        "未登记的 ID 标 orphan",
        [d["kind"] for d in payload5["whitelist_detail"] if d["id"] == "NEVER"],
        ["orphan"],
    )

    # 停用的机器人：会话仍然列出、标 bot_unavailable、绝不发请求
    book6 = mod.SessionBook(FakePlugin2([]), PLUGIN_DIR)
    book6._loaded = True
    book6._db_seeded = True
    book6.bot_registry = {
        "BOT_A": {"adapter": "qq_official"},
        "BOT_GONE": {"adapter": "qq_official"},
    }
    book6.sessions = {
        "BOT_GONE:GroupMessage:GX": {
            "umo": "BOT_GONE:GroupMessage:GX",
            "platform_id": "BOT_GONE",
            "platform_name": "qq_official",
            "adapter": "qq_official",
            "message_type": "GroupMessage",
            "session_id": "GX",
            "group_openid": "GX",
            "last_seen": 0.0,
            "msg_count": 0,
        }
    }

    async def _boom(*args, **kwargs):
        raise AssertionError("停用的机器人不应该发任何请求")

    book6.group_meta = _boom
    payload6 = asyncio.run(book6.build_payload())
    row6 = payload6["sessions"][0]
    eq("停用机器人的会话仍然列出", row6["kind"], "group")
    eq("标成未启用", row6["error_kind"], "bot_unavailable")
    eq("行上标记 bot_enabled=False", row6["bot_enabled"], False)
    eq(
        "bots 里也标未启用",
        [b["enabled"] for b in payload6["bots"] if b["platform_id"] == "BOT_GONE"],
        [False],
    )
    check("群名兜底说明是哪个机器人", "BOT_GONE" in row6["name"])

    # 工作区补种：只有一个机器人时才敢猜
    import sys as _sys

    ws_root = Path(tempfile.mkdtemp())
    (ws_root / "_FriendMessage_U1").mkdir()
    stub_names = [
        "astrbot.core",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    ]
    _saved = {name: _sys.modules.get(name) for name in stub_names}
    _astrbot_mod = _sys.modules["astrbot"]
    _saved_core = getattr(_astrbot_mod, "core", None)
    _core = types.ModuleType("astrbot.core")
    _utils = types.ModuleType("astrbot.core.utils")
    _apath = types.ModuleType("astrbot.core.utils.astrbot_path")
    _apath.get_astrbot_workspaces_path = lambda: str(ws_root)
    _sys.modules["astrbot.core"] = _core
    _sys.modules["astrbot.core.utils"] = _utils
    _sys.modules["astrbot.core.utils.astrbot_path"] = _apath
    _astrbot_mod.core = _core
    _core.utils = _utils
    _utils.astrbot_path = _apath
    try:
        one = mod.SessionBook(FakePlugin([]), PLUGIN_DIR)
        one._loaded = True
        one.seed_workspaces()
        eq("单机器人时从工作区补出私聊", "茜特菈莉:FriendMessage:U1" in one.sessions, True)

        two = mod.SessionBook(FakePlugin2([]), PLUGIN_DIR)
        two._loaded = True
        two.seed_workspaces()
        eq("多机器人时不猜工作区归属", len(two.sessions), 0)
    finally:
        for name, old in _saved.items():
            if old is None:
                _sys.modules.pop(name, None)
            else:
                _sys.modules[name] = old
        if _saved_core is None:
            try:
                delattr(_astrbot_mod, "core")
            except AttributeError:
                pass
        else:
            _astrbot_mod.core = _saved_core

    print(f"\n断言通过 {PASSED} 条")
    if FAILED:
        print(f"失败 {len(FAILED)} 条：")
        for item in FAILED:
            print("  [FAIL]", item)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
