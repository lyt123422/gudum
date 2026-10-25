"""破甲（回答尺度）回归测试（离线）：不碰真机、不碰真实 .env / data。

运行：.venv\\Scripts\\python.exe -m unittest discover tests -v
覆盖：
  单元  —— 状态优先级、预设切换、红线段恒在、暂停后不生效、非法预设拒绝、落盘持久化；
  集成  —— 真实走 _do_chat 管线，截获发给模型的 messages，验证条款真的进了请求体
          （这是"改了没生效"最关键的一环：函数返回对 ≠ 请求里有）；
  人设  —— 破甲生效时 ANTI_JAILBREAK 必须被抑制，否则两份"最高优先级"打架；
  面板  —— /api/jailbreak 系列接口的状态形状与自检结论。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("PANEL_NO_OPEN", "1")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-offline")
os.environ.setdefault("SUPERUSERS", '["123456"]')
os.environ.setdefault("COMMAND_START", '["","/"]')

from app import jailbreak


class JailbreakBase(unittest.TestCase):
    """每个用例都在临时目录里跑，绝不碰真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patcher_state = mock.patch.object(jailbreak, "STATE_FILE",
                                               self.root / "jailbreak.json")
        self.patcher_state.start()
        self.patcher_cache = mock.patch.dict(jailbreak._CACHE,
                                             {"at": 0.0, "data": {}})
        self.patcher_cache.start()
        self.addCleanup(self.patcher_state.stop)
        self.addCleanup(self.patcher_cache.stop)
        self.addCleanup(self.tmp.cleanup)

    def _set_enabled(self, on: bool):
        return mock.patch.object(jailbreak, "enabled", lambda: on)

    def _set_paused(self, on: bool):
        return mock.patch.object(jailbreak, "paused", lambda: on)


class TestActiveState(JailbreakBase):
    """生效 = 开关开 且 未暂停。"""

    def test_default_active(self):
        with self._set_enabled(True), self._set_paused(False):
            self.assertTrue(jailbreak.is_active())

    def test_paused_blocks(self):
        with self._set_enabled(True), self._set_paused(True):
            self.assertFalse(jailbreak.is_active())

    def test_disabled_blocks(self):
        with self._set_enabled(False), self._set_paused(False):
            self.assertFalse(jailbreak.is_active())

    def test_default_enabled_is_on(self):
        """启动自动破甲：插件开关默认 True。"""
        from app import plugin_manager
        entry = next(b for b in plugin_manager.BUILTIN if b["id"] == "jailbreak")
        self.assertTrue(entry["default"])


class TestPresetPersistence(JailbreakBase):
    def test_default_preset(self):
        self.assertEqual(jailbreak.preset(), "标准")

    def test_set_valid_preset(self):
        self.assertTrue(jailbreak.set_preset("技术直答"))
        self.assertEqual(jailbreak.preset(), "技术直答")

    def test_reject_unknown_preset(self):
        self.assertFalse(jailbreak.set_preset("不存在"))
        self.assertEqual(jailbreak.preset(), "标准")

    def test_preset_survives_reload(self):
        jailbreak.set_preset("温和")
        jailbreak._CACHE.update(at=0.0, data={})   # 强制重读磁盘
        self.assertEqual(jailbreak.preset(), "温和")

    def test_paused_survives_reload(self):
        """纯手动暂停：重启后仍保持暂停。"""
        jailbreak.set_paused(True)
        jailbreak._CACHE.update(at=0.0, data={})
        self.assertTrue(jailbreak.paused())

    def test_corrupt_state_falls_back(self):
        (self.root / "jailbreak.json").write_text("{ not json", encoding="utf-8")
        jailbreak._CACHE.update(at=0.0, data={})
        self.assertEqual(jailbreak.preset(), "标准")
        self.assertFalse(jailbreak.paused())


class TestClauseContent(JailbreakBase):
    def test_redlines_always_present(self):
        """红线段在任何预设下都必须存在。"""
        for name in jailbreak.PRESETS:
            clause = jailbreak.render_clause(name)
            self.assertIn("卡密", clause, f"{name} 缺少授权红线")
            self.assertIn("勒索", clause, f"{name} 缺少木马红线")

    def test_preset_changes_body_only(self):
        std = jailbreak.render_clause("标准")
        tech = jailbreak.render_clause("技术直答")
        self.assertNotEqual(std, tech)
        for clause in (std, tech):
            self.assertIn("【回答尺度", clause)
            self.assertIn("【不可覆盖的红线", clause)

    def test_all_presets_renderable(self):
        for name in jailbreak.PRESETS:
            self.assertTrue(jailbreak.render_clause(name).strip())

    def test_status_shape(self):
        st = jailbreak.status()
        for key in ("enabled", "paused", "active", "preset", "presets"):
            self.assertIn(key, st)
        self.assertIsInstance(st["presets"], list)


class TestPersonaIntegration(unittest.TestCase):
    """破甲生效时必须抑制人设自带的 ANTI_JAILBREAK，否则两份"最高优先级"打架。"""

    def test_anti_jailbreak_present_by_default(self):
        from app import personas
        p = personas.Persona(name="t", text="你是猫娘", examples=[])
        self.assertIn("设定守则", personas.compose_system(p))

    def test_anti_jailbreak_suppressed(self):
        from app import personas
        p = personas.Persona(name="t", text="你是猫娘", examples=[])
        out = personas.compose_system(p, suppress_anti_jailbreak=True)
        self.assertNotIn("设定守则", out)
        self.assertIn("你是猫娘", out)   # 人设本体必须保留

    def test_persona_file_can_opt_out(self):
        """人设文件里「# 防越狱: 关」仍然生效。"""
        from app import personas
        raw = "# 名称: t\n# 防越狱: 关\n\n[人设]\n你是猫娘\n"
        p = personas.parse_persona(raw, "t")
        self.assertNotIn("设定守则", personas.compose_system(p))




# ==================== 集成测试：条款是否真的进了请求体 ====================
#
# 这一层是本轮实测的教训：函数返回 True 不代表请求里带上了条款。
# 做法是真实调用 chat._do_chat，把 client.chat 换成捕获器，检查 messages。

class _FakeMatcher:
    """够用的 matcher 替身：记录发出去的内容，finish 不抛异常。"""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(str(content))

    async def finish(self, content=None):
        if content is not None:
            self.sent.append(str(content))


class _FakeEvent:
    def __init__(self, uid: str, message_type: str = "private"):
        self.user_id = int(uid)
        self.message_type = message_type
        self.group_id = 999

    def get_plaintext(self):
        return ""


class ChatPipelineBase(unittest.TestCase):
    """把 _do_chat 的副作用全部打桩，只留下 messages 组装这条路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        # nonebot 初始化（幂等）：chat 模块导入时要用 on_command
        import nonebot
        try:
            nonebot.get_driver()
        except ValueError:
            nonebot.init()

        starter = [
            mock.patch.object(jailbreak, "STATE_FILE", self.root / "jb.json"),
            mock.patch.dict(jailbreak._CACHE, {"at": 0.0, "data": {}}),
            mock.patch.object(jailbreak, "enabled", lambda: True),
            mock.patch.object(jailbreak, "paused", lambda: False),
        ]
        for p in starter:
            p.start()
            self.addCleanup(p.stop)

        from app.plugins import chat as chat_mod
        self.chat = chat_mod
        self.captured: list[list[dict]] = []

        async def fake_chat(messages, temperature=None, tools=None, tool_executor=None):
            self.captured.append(messages)
            return "好的"

        self.chat.client.chat = fake_chat
        # 工具调用路径也要走同一个离线假客户端，禁止测试打真实 API
        self.chat.client.chat_with_tools = fake_chat
        body = [
            mock.patch.object(self.chat, "_allow_user", lambda uid: True),
            mock.patch.object(self.chat, "_allow_group", lambda gid: True),
            mock.patch.object(self.chat, "memory_remember", lambda *a, **k: None),
            mock.patch.object(self.chat, "memory_history", lambda key: []),
            mock.patch.object(self.chat, "_MEMORY_DIR", self.root / "mem"),
            mock.patch.object(self.chat, "_nicknames", dict),
        ]
        for p in body:
            p.start()
            self.addCleanup(p.stop)

        # 固定人设：带 address 与示例，确保注入顺序可验证
        from app import personas
        self.persona = personas.Persona(
            name="测试人设", text="你是测试人设，说话简短。",
            examples=[("你好", "在")], greeting="", address="主人")
        pp = mock.patch.object(personas, "get_active", lambda: self.persona)
        pp.start()
        self.addCleanup(pp.stop)

    def run_chat(self, uid: str = "123456", message_type: str = "private"):
        matcher = _FakeMatcher()
        event = _FakeEvent(uid, message_type)
        asyncio.run(self.chat._do_chat(matcher, event, "测试问题"))
        self.assertTrue(self.captured, "没捕获到模型请求，管线没走到调用")
        return self.captured[-1]

    def system_text(self, messages) -> str:
        return "\n".join(m["content"] for m in messages
                         if m.get("role") == "system"
                         and isinstance(m.get("content"), str))


class TestPipelineInjection(ChatPipelineBase):
    """破甲条款必须真的出现在发给模型的 system 消息里。"""

    def test_admin_gets_clause(self):
        sys_text = self.system_text(self.run_chat(uid="123456"))
        self.assertIn("【回答尺度", sys_text)
        self.assertIn("【不可覆盖的红线", sys_text)
        self.assertIn("卡密", sys_text)

    def test_admin_clause_suppresses_anti_jailbreak(self):
        sys_text = self.system_text(self.run_chat(uid="123456"))
        self.assertNotIn("设定守则", sys_text)
        self.assertIn("你是测试人设", sys_text)   # 人设本体保留

    def test_non_admin_gets_normal_persona(self):
        """scope=admin 时，非管理员不应拿到破甲条款。"""
        with mock.patch.object(self.chat, "JAILBREAK_SCOPE", "admin"):
            sys_text = self.system_text(self.run_chat(uid="999999"))
        self.assertNotIn("【回答尺度", sys_text)
        self.assertIn("设定守则", sys_text)      # 普通人设的防越狱条款还在

    def test_scope_all_grants_everyone(self):
        with mock.patch.object(self.chat, "JAILBREAK_SCOPE", "all"):
            sys_text = self.system_text(self.run_chat(uid="999999"))
        self.assertIn("【回答尺度", sys_text)

    def test_paused_removes_clause(self):
        with mock.patch.object(jailbreak, "paused", lambda: True):
            sys_text = self.system_text(self.run_chat(uid="123456"))
        self.assertNotIn("【回答尺度", sys_text)
        self.assertIn("设定守则", sys_text)

    def test_disabled_removes_clause(self):
        with mock.patch.object(jailbreak, "enabled", lambda: False):
            sys_text = self.system_text(self.run_chat(uid="123456"))
        self.assertNotIn("【回答尺度", sys_text)

    def test_preset_reaches_request(self):
        jailbreak.set_preset("技术直答")
        sys_text = self.system_text(self.run_chat(uid="123456"))
        self.assertIn("不主动补安全提示", sys_text)

    def test_clause_is_in_first_system_message(self):
        """条款在 messages[0]（部分服务商要求 system 在最前）。"""
        messages = self.run_chat(uid="123456")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("【回答尺度", messages[0]["content"])

    def test_group_admin_also_gets_clause(self):
        sys_text = self.system_text(self.run_chat(uid="123456", message_type="group"))
        self.assertIn("【回答尺度", sys_text)

    def test_address_line_survives(self):
        """破甲不应破坏「称呼」注入。"""
        sys_text = self.system_text(self.run_chat(uid="123456"))
        self.assertIn("主人", sys_text)


class TestHealthzField(unittest.TestCase):
    """main.py 的 healthz 必须暴露破甲状态，否则面板无法识别旧内核代码。"""

    def test_main_has_jailbreak_block(self):
        src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
            encoding="utf-8")
        self.assertIn('payload["jailbreak"]', src)
        self.assertIn("_jb.status()", src)


class TestSelftestLogic(unittest.TestCase):
    """自检要能识别三类问题：开关关、暂停、内核旧代码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(jailbreak, "STATE_FILE", self.root / "jb.json"),
                  mock.patch.dict(jailbreak._CACHE, {"at": 0.0, "data": {}})):
            p.start()
            self.addCleanup(p.stop)

    def _selftest(self, *, enabled: bool, paused: bool,
                  kernel_payload: dict | None, scope: str = "admin",
                  supers: str = '["123456"]'):
        from app import panel
        env = {"JAILBREAK_SCOPE": scope, "SUPERUSERS": supers}
        fake_resp = mock.Mock(status_code=200)
        fake_resp.json.return_value = kernel_payload or {}
        with mock.patch.object(jailbreak, "enabled", lambda: enabled), \
             mock.patch.object(jailbreak, "paused", lambda: paused), \
             mock.patch.object(panel, "load_env", lambda _p: env), \
             mock.patch.object(panel.httpx, "get", lambda *a, **k: fake_resp):
            return panel.api_jailbreak_selftest()

    def test_all_good(self):
        out = self._selftest(
            enabled=True, paused=False,
            kernel_payload={"plugins": ["chat"], "jailbreak": {"active": True}})
        self.assertTrue(out["ok"], out["report"])
        self.assertNotIn("发现的问题", out["report"])

    def test_detects_old_kernel(self):
        """healthz 没有 jailbreak 字段 = 内核跑的是旧代码。"""
        out = self._selftest(
            enabled=True, paused=False,
            kernel_payload={"plugins": ["chat", "douyin", "memes"]})
        self.assertFalse(out["ok"])
        self.assertIn("旧代码", out["report"])

    def test_detects_paused(self):
        out = self._selftest(
            enabled=True, paused=True,
            kernel_payload={"plugins": ["chat"], "jailbreak": {"active": False}})
        self.assertFalse(out["ok"])
        self.assertIn("暂停", out["report"])

    def test_detects_placeholder_admin(self):
        out = self._selftest(
            enabled=True, paused=False,
            kernel_payload={"plugins": ["chat"], "jailbreak": {"active": True}},
            supers='["10000"]')
        self.assertFalse(out["ok"])
        self.assertIn("管理员", out["report"])

    def test_detects_kernel_down(self):
        from app import panel
        env = {"JAILBREAK_SCOPE": "admin", "SUPERUSERS": '["123456"]'}

        def boom(*a, **k):
            raise RuntimeError("connection refused")

        with mock.patch.object(jailbreak, "enabled", lambda: True),              mock.patch.object(jailbreak, "paused", lambda: False),              mock.patch.object(panel, "load_env", lambda _p: env),              mock.patch.object(panel.httpx, "get", boom):
            out = panel.api_jailbreak_selftest()
        self.assertFalse(out["ok"])
        self.assertIn("内核没在运行", out["report"])


class TestEmptyContentRetry(unittest.TestCase):
    """推理模型思考吃满预算 → 正文为空。必须放宽预算重试，不能当成拒绝。

    实测环境：gemini-test-flash，max_tokens=2048 时约 1/3 概率返回空正文
    （reasoning_tokens 吃掉全部预算，finish_reason=length）。
    """

    def _client(self):
        from app.deepseek_api import DeepSeekClient
        return DeepSeekClient(base_url="http://127.0.0.1:1/v1", api_key="k",
                              model="m", timeout=1, max_retries=0)

    def _resp(self, content: str, finish: str = "stop", reasoning: int = 0):
        resp = mock.Mock()
        choice = mock.Mock()
        choice.finish_reason = finish
        choice.message.content = content
        resp.choices = [choice]
        resp.usage.completion_tokens_details.reasoning_tokens = reasoning
        return resp

    def test_empty_with_reasoning_retries_bigger_budget(self):
        c = self._client()
        calls = []

        async def fake_create(messages, temperature, max_tokens):
            calls.append(max_tokens)
            return self._resp("" if len(calls) == 1 else "真正的内容",
                              finish="length", reasoning=2048)

        async def run():
            with mock.patch.object(c, "_create", fake_create):
                return await c.chat([{"role": "user", "content": "x"}])

        self.assertEqual(asyncio.run(run()), "真正的内容")
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[1], calls[0])   # 必须放宽预算

    def test_genuine_empty_still_errors(self):
        """两次都空 → 报错，不能无限重试。"""
        from app.deepseek_api import DeepSeekError
        c = self._client()

        async def fake_create(messages, temperature, max_tokens):
            return self._resp("", finish="length", reasoning=2048)

        async def run():
            with mock.patch.object(c, "_create", fake_create):
                return await c.chat([{"role": "user", "content": "x"}])

        with self.assertRaises(DeepSeekError):
            asyncio.run(run())

    def test_normal_reply_no_retry(self):
        c = self._client()
        calls = []

        async def fake_create(messages, temperature, max_tokens):
            calls.append(max_tokens)
            return self._resp("直接回答")

        async def run():
            with mock.patch.object(c, "_create", fake_create):
                return await c.chat([{"role": "user", "content": "x"}])

        self.assertEqual(asyncio.run(run()), "直接回答")
        self.assertEqual(len(calls), 1)          # 正常路径不该多花一次钱

    def test_default_budget_is_4096(self):
        """默认预算必须高于旧的 2048——那是空回复的根因。"""
        from app.deepseek_api import DeepSeekClient
        c = DeepSeekClient(base_url="http://x/v1", api_key="k", model="m")
        self.assertGreaterEqual(c.max_tokens, 4096)
