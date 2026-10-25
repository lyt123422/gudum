"""OneBot v11 模拟客户端：不登录 QQ，直接连内核反向 WS 端到端测试聊天管线。

用法（内核需已启动）:
    .venv\\Scripts\\python.exe tools\\mock_onebot_test.py

流程: 连上 ws://127.0.0.1:8081/onebot/v11/ws → 依次发「帮助」「状态」私聊消息
→ 收集机器人回话断言。聊天消息在未配 API Key 时应回复友好的 Key 错误提示，
从而覆盖: 私聊路由 → 限频 → 人设/记忆组装 → DeepSeek 调用 → 错误翻译 → 回信。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.envutil import load_env  # noqa: E402

WS_URL = "ws://127.0.0.1:8081/onebot/v11/ws"
ADMIN_QQ = 10000  # 与 .env 的 SUPERUSERS 对应
# 专属假 self_id：绝不能与真实登录的 QQ 相同，否则适配器判重直接 403
SELF_ID = 999000999


def private_event(text: str, user_id: int = ADMIN_QQ, seq: int = 1) -> str:
    return json.dumps({
        "time": 1760000000, "self_id": SELF_ID, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "user_id": user_id,
        "message_id": 900000 + seq,   # 必填；缺了会被适配器降级成无消息事件
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0,
        "sender": {"user_id": user_id, "nickname": "E2E测试"}})


async def next_bot_reply(ws) -> str:
    """收到 send_private_msg 即视为回话；其他 action(get_login_info等)直接应答放行。"""
    while True:
        raw = await ws.recv()
        frame = json.loads(raw)
        print(f"    [帧] action={frame.get('action')} echo={frame.get('echo')} "
              f"预览={str(frame.get('params', {}))[:80]}")
        if frame.get("post_type") or not frame.get("action"):
            continue
        echo = frame.get("echo")
        if frame["action"] in ("send_msg", "send_private_msg"):
            if echo:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo}))
            message = frame.get("params", {}).get("message")
            if isinstance(message, str):        # 字符串格式回信
                text = message
            else:                                # 段数组格式回信
                text = "".join(s.get("data", {}).get("text", "")
                               for s in message if isinstance(s, dict))
            if "机器人已上线" in text:   # 重连时的上线通知，不是本次用例的回话
                continue
            return text
        if echo:   # get_login_info 等探活调用
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {},
                                      "echo": echo}))


async def main() -> int:
    env = load_env(ROOT / ".env")
    token = env.get("ONEBOT_ACCESS_TOKEN", "")
    url = WS_URL + (f"?access_token={token}" if token else "")
    headers = {"X-Self-ID": str(SELF_ID)}   # 反向 WS 必带；专属假ID避免与真实QQ撞号
    if token:
        headers["Authorization"] = f"Bearer {token}"
    key_set = bool(env.get("DEEPSEEK_API_KEY"))
    # 「我是主人」会改写 .env 与 data/admins.json：记录原始状态，测完还原
    original_su = next((ln for ln in (ROOT / ".env").read_text(
        encoding="utf-8").splitlines() if ln.startswith("SUPERUSERS=")), None)
    admins_file = ROOT / "data" / "admins.json"
    original_admins = admins_file.read_text(encoding="utf-8") if admins_file.exists() else None
    claim_file = ROOT / "data" / "claim_code.txt"
    original_claim = claim_file.read_text(encoding="utf-8") if claim_file.exists() else None
    nick_file = ROOT / "data" / "nicknames.json"
    original_nicks = nick_file.read_text(encoding="utf-8") if nick_file.exists() else None
    claim_code = original_claim.strip() if original_claim else ""
    # 环境里可能已有真实管理员（比如用户主号已认领）：测试号 10000 非管理员时
    # 「状态」按设计静默，该用例自动跳过
    import json as _json
    try:
        su_list = [str(x).strip('" ') for x in _json.loads(env.get("SUPERUSERS", "[]"))]
    except ValueError:
        su_list = [env.get("SUPERUSERS", "").strip('[]" ')]
    test_is_admin = str(ADMIN_QQ) in su_list
    cases = [("我是主人 " + claim_code if claim_code else "我是主人",
              ["你已成为管理员", "已经有管理员", "认领码"]),
             ("帮助", "gudum"),
             ("人设列表", "人设库")]
    if test_is_admin:
        cases.append(("状态", "机器人状态"))
    else:
        print("(SKIP 状态用例：已有真实管理员，测试号非管理员，静默属正确行为)")
    cases.append(("表情列表", "表情"))
    cases.append(("叫我 测试员", "记住"))
    cases.append(("刷抖音", ["插件", "管理员"]))   # 未开启→装插件提示；已开启→仅管理员
    cases.append(("你好呀，这是端到端测试", None))
    ok = False
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await asyncio.sleep(1.5)
            for seq, (text, expect) in enumerate(cases, 1):
                await ws.send(private_event(text, seq=seq))
                try:
                    reply = await asyncio.wait_for(next_bot_reply(ws), timeout=45)
                except TimeoutError:
                    print(f"✗ [{text}] 45 秒内没有收到机器人回话")
                    break
                expects = expect if isinstance(expect, (list, tuple)) else [expect]
                if expect and not any(e in reply for e in expects):
                    print(f"✗ [{text}] 回复不含预期内容 {expects}: {reply[:100]}")
                    break
                if text.startswith("你好") and not key_set and "API Key" not in reply:
                    print(f"✗ [{text}] 未配 Key 时应回 Key 错误提示: {reply[:100]}")
                    break
                print(f"✓ [{text}] -> {reply[:70]}")
            else:
                ok = True
    finally:
        # 清理测试残留：会话记忆 + SUPERUSERS/admins/认领码 还原（异常也执行）
        (ROOT / "data" / "memory" / f"p{ADMIN_QQ}.json").unlink(missing_ok=True)
        if original_su is not None:
            env_file = ROOT / ".env"
            lines = [original_su if ln.startswith("SUPERUSERS=") else ln
                     for ln in env_file.read_text(encoding="utf-8").splitlines()]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if original_admins is not None:
            admins_file.write_text(original_admins, encoding="utf-8")
        elif admins_file.exists():
            admins_file.unlink()
        if original_claim is not None:
            claim_file.write_text(original_claim, encoding="utf-8")
        elif claim_file.exists():
            claim_file.unlink()
        if original_nicks is not None:
            nick_file.write_text(original_nicks, encoding="utf-8")
        elif nick_file.exists():
            nick_file.unlink()
    print("\n=== 端到端聊天管线全部通过 ===" if ok else "\n=== 存在失败用例 ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
