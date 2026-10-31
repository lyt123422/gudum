"""模拟真实用户在控制台配置模型的全流程（离线：临时 .env / 临时档案，绝不碰真实环境）。

跑法：.venv\\Scripts\\python.exe tools/sim_model_config.py
每个场景打印：用户操作 → 系统实际行为（接口返回 + .env / providers.json 落盘结果）。
内核重启一律 mock 掉（_pid_on_port→None / _running→False），脚本只验证"保存"语义。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BANNER = "\n" + "=" * 72 + "\n"


def setup_env(tmp: Path, text: str) -> Path:
    env = tmp / ".env"
    env.write_text(text, encoding="utf-8")
    return env


def snapshot(tmp: Path, env: Path) -> dict:
    return {
        "env": env.read_text(encoding="utf-8") if env.exists() else "",
        "providers": json.loads((tmp / "providers.json").read_text(encoding="utf-8"))
        if (tmp / "providers.json").exists() else {},
    }


def show(title: str, before: dict, resp: dict, after: dict) -> None:
    print(BANNER + f"【{title}】")
    print(f"  接口返回: {json.dumps(resp, ensure_ascii=False)}")
    if before["env"] != after["env"]:
        import difflib
        diff = [l for l in difflib.unified_diff(
            before["env"].splitlines(), after["env"].splitlines(), lineterm="", n=0)
            if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        print("  .env 变化: " + " | ".join(d[:60] for d in diff))
    if before["providers"] != after["providers"]:
        print(f"  providers.json: {json.dumps(after['providers'], ensure_ascii=False)[:300]}")
    if before == after:
        print("  (无落盘变化)")


def main() -> int:
    from fastapi.testclient import TestClient
    from app import panel

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = setup_env(tmp, "SUPERUSERS=[\"10000\"]\n")
        # 隔离：.env/档案指向临时目录；永不触发内核重启；反代路径探测短路
        patches = [
            mock.patch.object(panel, "ENV_FILE", env),
            mock.patch.object(panel, "PROVIDERS_FILE", tmp / "providers.json"),
            mock.patch.object(panel, "_pid_on_port", lambda port: None),
            mock.patch.object(panel, "_running", lambda name: False),
            mock.patch.object(panel, "_restart_kernel_if_running",
                              mock.AsyncMock(return_value=False)),
        ]
        import app.proxy_manager as pm
        patches += [
            mock.patch.object(pm, "PROXY_FILE", tmp / "proxy.json"),
            mock.patch.object(pm, "_resolve_path", lambda preferred="": preferred or ""),
        ]
        for p in patches:
            p.start()

        client = TestClient(panel.app, base_url="http://127.0.0.1:8090")

        def post(path: str, body: dict) -> dict:
            return client.post(path, json=body).json()

        def snap() -> dict:
            return snapshot(tmp, env)

        # ---- 场景 1：全新用户打开面板 ----
        c = client.get("/api/config").json()
        p = client.get("/api/providers").json()
        print(BANNER + "【场景1 · 新手首次打开模型配置】")
        print(f"  /api/config: base={c['base_url']!r} key掩码={c['api_key']!r} "
              f"model={c['model']!r} live_provider={c['live_provider']!r}")
        print(f"  /api/providers.saved 初始档案: {list(p['saved'])}")

        # ---- 场景 2：点 DeepSeek 胶囊 → 填 Key → 保存 ----
        before = snap()
        r = post("/api/save", {"provider": "DeepSeek",
                               "base_url": "https://api.deepseek.com",
                               "api_key": "sk-user-real-key-123456",
                               "model": "deepseek-chat"})
        show("场景2 · 首次配置 DeepSeek（填 Key 保存）", before, r, snap())
        c = client.get("/api/config").json()
        print(f"  保存后 GET /api/config: key掩码={c['api_key']!r} live={c['live_provider']!r}")

        # ---- 场景 3：用户只想改管理员 QQ，点 QQ 卡片的保存 ----
        before = snap()
        r = post("/api/save", {"provider": "DeepSeek",
                               "base_url": "https://api.deepseek.com",
                               "model": "deepseek-chat",
                               "superuser": "111222333"})
        show("场景3 · 只改管理员 QQ（但前端把模型字段一并送上）", before, r, snap())

        # ---- 场景 4：点胶囊切 GLM，但忘了填 Key，直接保存 ----
        before = snap()
        r = post("/api/save", {"provider": "智谱GLM",
                               "base_url": "https://open.bigmodel.cn/api/paas/v4",
                               "model": "glm-4-flash"})
        show("场景4 · 切 GLM 忘填 Key 就保存", before, r, snap())
        c = client.get("/api/config").json()
        print(f"  此时 .env 实际状态: base={c['base_url']!r} key_set={c['api_key_set']} "
              f"model={c['model']!r}")

        # ---- 场景 5：切 GLM 填 Key 保存 → 再切回 DeepSeek（不填 Key）----
        before = snap()
        r = post("/api/save", {"provider": "智谱GLM",
                               "base_url": "https://open.bigmodel.cn/api/paas/v4",
                               "api_key": "glm-user-key-abc",
                               "model": "glm-4-flash"})
        show("场景5a · GLM 填 Key 保存", before, r, snap())
        before = snap()
        r = post("/api/save", {"provider": "DeepSeek",
                               "base_url": "https://api.deepseek.com",
                               "model": "deepseek-chat"})
        show("场景5b · 切回 DeepSeek（不填 Key，靠档案）", before, r, snap())
        c = client.get("/api/config").json()
        print(f"  DeepSeek 的 Key 恢复了吗: key掩码={c['api_key']!r}")

        # ---- 场景 6：不点胶囊，直接把 base_url 改成 GLM 再保存（档案归属）----
        before = snap()
        r = post("/api/save", {"provider": "DeepSeek",     # 前端高亮还在 DeepSeek
                               "base_url": "https://open.bigmodel.cn/api/paas/v4",
                               "api_key": "glm-key-xyz",
                               "model": "glm-4-flash"})
        show("场景6 · 高亮 DeepSeek 却填了 GLM 的地址（不点胶囊）", before, r, snap())
        print(f"  → DeepSeek 档案现在: {json.dumps(
            json.loads((tmp / 'providers.json').read_text(encoding='utf-8')).get('DeepSeek'),
            ensure_ascii=False)}")

        # ---- 场景 7：反代激活期改直连，然后停止反代 ----
        (tmp / "proxy.json").write_text(json.dumps({
            "path": "", "port": 8788, "api_key": "hub-key",
            "model": "deepseek-v4.1-flash", "active": True,
            "previous": {"base_url": "https://api.deepseek.com",
                         "api_key": "sk-old-direct-key", "model": "deepseek-chat"},
        }), encoding="utf-8")
        pm._cfg_cache.update(at=0.0, data=None)   # 模拟用户过了 2 秒才操作（缓存过期）
        before = snap()
        r = post("/api/save", {"provider": "",                     # 用户不选胶囊
                               "base_url": "https://api.deepseek.com",
                               "api_key": "sk-NEW-direct-key",     # 用户换了新 Key
                               "model": "deepseek-chat"})
        show("场景7a · 反代激活时保存直连新 Key（应只存档不激活）", before, r, snap())
        before = snap()
        r = post("/api/proxy/stop", {})
        show("场景7b · 停止反代「切回直连」", before, r, snap())
        c = client.get("/api/config").json()
        print(f"  切回后的直连 Key 是新的吗: key掩码={c['api_key']!r} "
              f"(NEW 掩码应含 sk-NEW-…，OLD 掩码含 sk-old-…)")
        print(f"  .env 现值: {[l for l in after_env(env).splitlines() if 'API_KEY' in l]}")

        # ---- 场景 8：切到 Ollama（本地无 Key）----
        before = snap()
        r = post("/api/save", {"provider": "Ollama本地",
                               "base_url": "http://localhost:11434/v1",
                               "model": "qwen2.5:7b"})
        show("场景8 · 切到 Ollama 本地（无 Key）", before, r, snap())
        print(f"  .env API_KEY 行: {[l for l in after_env(env).splitlines() if 'API_KEY' in l]}")

    print(BANNER + "模拟结束")
    return 0


def after_env(env: Path) -> str:
    return env.read_text(encoding="utf-8") if env.exists() else ""


if __name__ == "__main__":
    raise SystemExit(main())
