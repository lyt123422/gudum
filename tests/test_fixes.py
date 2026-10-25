"""修复回归测试（离线）：不碰真实 .env / QQ / 浏览器。

运行：.venv\\Scripts\\python.exe -m unittest discover tests -v
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class TestMaskedKey(unittest.TestCase):
    """评审 P0-1：掩码 Key 必须被识别并拒绝。"""

    def test_masked_variants_rejected(self):
        from app.panel import _looks_masked
        self.assertTrue(_looks_masked("sk-b23…23e8"))
        self.assertTrue(_looks_masked("sk-b23...23e8"))
        self.assertFalse(_looks_masked("sk-realkey123456"))
        self.assertFalse(_looks_masked(""))

    def test_save_rejects_masked_key(self):
        from fastapi.testclient import TestClient
        from app import panel
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('DEEPSEEK_API_KEY="sk-realkey123456"\n'
                           'DEEPSEEK_MODEL="deepseek-chat"\n', encoding="utf-8")
            client = TestClient(panel.app, base_url="http://127.0.0.1:8090")
            r = client.post("/api/save", json={"api_key": "sk-b23…23e8"})
            self.assertFalse(r.json()["ok"])
            self.assertIn("掩码", r.json()["error"])
            self.assertIn("sk-realkey123456", env.read_text(encoding="utf-8"))


class TestSaveChangedDetection(unittest.TestCase):
    """评审 P1-1：before 快照在写入前，changed 才真实；重启结果如实返回。"""

    def _client(self, tmp: Path):
        from fastapi.testclient import TestClient
        from app import panel
        env = Path(tmp) / ".env"
        env.write_text('DEEPSEEK_API_KEY="sk-realkey123456"\n'
                       'DEEPSEEK_MODEL="deepseek-chat"\n'
                       'DEEPSEEK_BASE_URL="https://api.deepseek.com"\n'
                       'NAPCAT_ACCOUNT="123456789"\n'
                       'SUPERUSERS=["10000"]\n', encoding="utf-8")
        started = {"n": 0}

        async def fake_restart():
            started["n"] += 1
            return True

        p = mock.patch.object(panel, "ENV_FILE", env)
        p2 = mock.patch.object(panel, "_running", lambda name: True)
        p3 = mock.patch.object(panel, "_pid_on_port", lambda port: 111)
        p4 = mock.patch.object(panel, "_restart_kernel_if_running", fake_restart)
        for x in (p, p2, p3, p4):
            x.start()
        self.addCleanup(lambda: [x.stop() for x in (p, p2, p3, p4)])
        return TestClient(panel.app, base_url="http://127.0.0.1:8090"), started

    def test_model_change_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, started = self._client(Path(tmp))
            r = client.post("/api/save", json={"model": "deepseek-reasoner"}).json()
            self.assertTrue(r["ok"])
            self.assertTrue(r["bot_restarted"])
            self.assertEqual(started["n"], 1)

    def test_account_only_no_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, started = self._client(Path(tmp))
            r = client.post("/api/save", json={"napcat_account": "987654321"}).json()
            self.assertTrue(r["ok"])
            self.assertFalse(r["bot_restarted"])
            self.assertEqual(started["n"], 0)

    def test_superusers_change_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, started = self._client(Path(tmp))
            r = client.post("/api/save", json={"superuser": "111111111,222222222"}).json()
            self.assertTrue(r["ok"])
            self.assertTrue(r["bot_restarted"])
            env_text = (Path(tmp) / ".env").read_text(encoding="utf-8")
            self.assertIn('["111111111", "222222222"]', env_text)


class TestEnvInjection(unittest.TestCase):
    """评审 P1-5 回归：换行注入仍被 envutil 拒绝。"""

    def test_newline_rejected(self):
        from app.envutil import EnvValueError, update_env
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("A=1\n", encoding="utf-8")
            with self.assertRaises(EnvValueError):
                update_env(env, {"A": 'x"\nEVIL=1'})
            self.assertEqual(env.read_text(encoding="utf-8").strip(), "A=1")


class TestVision(unittest.TestCase):
    """评审 P1-5：域名白名单、魔数、base64 转换。"""

    def test_host_allowed(self):
        from app import vision
        self.assertTrue(vision.host_allowed("https://gchat.qpic.cn/a.jpg"))
        self.assertTrue(vision.host_allowed("https://multimedia.nt.qq.com.cn/x"))
        self.assertFalse(vision.host_allowed("https://evil.com/a.jpg"))
        self.assertFalse(vision.host_allowed("not a url"))

    def test_sniff_mime(self):
        from app import vision
        self.assertEqual(vision.sniff_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 100),
                         "image/png")
        self.assertEqual(vision.sniff_mime(b"\xff\xd8\xff" + b"x" * 100),
                         "image/jpeg")
        self.assertEqual(vision.sniff_mime(b"hello"), "")

    def test_fetch_to_data_url(self):
        from app import vision
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 300

        class FakeResp:
            content = png
            headers = {}

            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return FakeResp()

        with mock.patch.object(vision.httpx, "AsyncClient", FakeClient):
            out = asyncio.run(vision.fetch_data_url("https://gchat.qpic.cn/a.png"))
        self.assertTrue(out.startswith("data:image/png;base64,"))

    def test_fetch_rejects_foreign_domain_before_network(self):
        from app import vision
        with self.assertRaises(ValueError):
            asyncio.run(vision.fetch_data_url("https://evil.com/a.png"))


class TestDailyLedgerRemoved(unittest.TestCase):
    """刷抖音功能已整体移除（2026-10-25 用户决策）：防回归——模块不得再出现。"""

    def test_douyin_module_gone(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "app" / "douyin.py").exists())
        self.assertFalse((root / "app" / "plugins" / "douyin.py").exists())


class TestRedactLog(unittest.TestCase):
    """日志脱敏回归（评审 P1-6）。"""

    def test_samples(self):
        from app.log_setup import redact_log
        text = redact_log("DEEPSEEK_API_KEY=sk-abcdefghijklmnop1234 done")
        self.assertNotIn("sk-abcdefghijklmnop1234", text)
        text = redact_log("Authorization: Bearer abc.def.ghi")
        self.assertNotIn("abc.def.ghi", text)
        self.assertIn("[REDACTED]", text)
        text = redact_log("password=topsecret1")
        self.assertNotIn("topsecret1", text)


class TestPanelSecurityRegression(unittest.TestCase):
    """CSRF/Host 防护回归（上一轮 P0-1 不得回退）。"""

    def test_csrf_layers(self):
        from fastapi.testclient import TestClient
        from app import panel
        client = TestClient(panel.app, base_url="http://127.0.0.1:8090")
        r = client.post("/api/plugin/toggle", content='{}',
                        headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 415)
        r = client.post("/api/plugin/toggle", json={"id": "memes", "enabled": True},
                        headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = client.post("/api/plugin/toggle", json={"id": "memes", "enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        r = client.get("/api/status", headers={"Host": "evil.com:8090"})
        self.assertEqual(r.status_code, 403)

    def test_config_masks_key(self):
        from fastapi.testclient import TestClient
        from app import panel
        client = TestClient(panel.app, base_url="http://127.0.0.1:8090")
        body = client.get("/api/config").json()
        key = body.get("api_key", "")
        self.assertTrue(key == "" or "…" in key)


if __name__ == "__main__":
    unittest.main()
