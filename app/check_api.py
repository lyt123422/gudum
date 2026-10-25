"""DeepSeek 连通性自检：.venv\\Scripts\\python.exe -m app.check_api

填好 .env 里的 DEEPSEEK_API_KEY 后跑一次，确认 Key、网络、模型都通。
"""
from __future__ import annotations

import time
from pathlib import Path

from openai import OpenAI

from .envutil import load_env


def main() -> int:
    env = load_env(Path(__file__).resolve().parents[1] / ".env")
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    base = env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = env.get("DEEPSEEK_MODEL") or "deepseek-chat"
    if not key:
        print("✗ 还没填 DEEPSEEK_API_KEY：编辑项目根目录的 .env 后再试")
        return 1
    masked = key[:6] + "…" + key[-4:] if len(key) > 12 else "…"
    print(f"· Base URL : {base}")
    print(f"· 模型     : {model}")
    print(f"· API Key  : {masked}")
    client = OpenAI(base_url=base, api_key=key, timeout=30)
    start = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "用一句话打个招呼"}],
            max_tokens=50,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 调用失败: {exc}")
        print("  常见原因: Key 抄错/未创建、账户余额不足、本机网络不通")
        return 1
    text = (resp.choices[0].message.content or "").strip()
    print(f"✓ 连通正常（耗时 {time.time() - start:.1f}s），DeepSeek 回复：{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
