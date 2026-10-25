"""共享助手状态：抖音真机会话 → 聊天 AI 的自我认知。

抖音会话线程写、聊天事件循环读，全程 threading.Lock；
只依赖标准库，避免与 nonebot/插件模块互相导入。

设计约定（DeepSeek v3 修复任务书）：
- 聊天 AI 每轮注入 render_self_state() 的第一人称状态，不再按关键词猜测；
- 管理员看细节（≤200 字），非管理员只看粗粒度，不暴露手机/视频/决策；
- reason 属模型自述：清洗格式字符 + 标注不可信，绝不当作指令；
- 措辞诚实：AI 只看过屏幕截图帧（没有声音），不得声称看完整个视频。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque

_LOCK = threading.Lock()
_EVENTS_MAX = 20
_SELF_STATE_MAX = 200

_state = {
    "connected": False,
    "running": False,
    "paused": False,
    "started_at": 0.0,
    "ended_at": 0.0,
    "plan_min": 0,
    "last_error": "",
    "session_id": "",
    "last_summary": "",
    "events": deque(maxlen=_EVENTS_MAX),
}


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"[\r\n【】<>「」\"']", " ", str(text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def set_connected(ok: bool, error: str = "") -> None:
    with _LOCK:
        _state["connected"] = bool(ok)
        if error:
            _state["last_error"] = _clean(error, 150)


def session_started(plan_min: int) -> None:
    with _LOCK:
        _state.update({
            "running": True,
            "paused": False,
            "started_at": time.time(),
            "ended_at": 0.0,
            "plan_min": int(plan_min),
            "last_error": "",
            "session_id": uuid.uuid4().hex[:8],
            "last_summary": "",
        })
        _state["events"].clear()


def set_paused(paused: bool) -> None:
    with _LOCK:
        _state["paused"] = bool(paused)


def record_event(action: str, reason: str = "", summary: str = "") -> None:
    with _LOCK:
        _state["events"].append({
            "ts": time.time(),
            "action": _clean(action, 10),
            "reason": _clean(reason, 40),
            "summary": _clean(summary, 30),
        })
        if summary:
            _state["last_summary"] = _clean(summary, 30)


def set_error(message: str) -> None:
    with _LOCK:
        _state["last_error"] = _clean(message, 150)


def session_stopped(reason: str = "") -> None:
    with _LOCK:
        _state["running"] = False
        _state["paused"] = False
        _state["ended_at"] = time.time()
        if reason:
            _state["last_error"] = _clean(reason, 150)


def snapshot() -> dict:
    with _LOCK:
        data = dict(_state)
        data["events"] = list(_state["events"])
        return data


def reset() -> None:
    with _LOCK:
        _state.update({
            "connected": False, "running": False, "paused": False,
            "started_at": 0.0, "ended_at": 0.0, "plan_min": 0,
            "last_error": "", "session_id": "", "last_summary": "",
        })
        _state["events"].clear()


def render_self_state(is_admin: bool) -> str:
    """给聊天 system prompt 用：第一人称；管理员看细节，其他人只看粗粒度。"""
    s = snapshot()
    if not is_admin:
        if s["running"]:
            return "【你当前的状态】你现在正在忙，只能简单聊两句。"
        return "【你当前的状态】你现在没在刷抖音。"

    now = time.time()
    if s["running"]:
        elapsed = max(0, int((now - s["started_at"]) / 60))
        state = "暂停中" if s["paused"] else "正在刷"
        lines = [f"【你当前的状态】你正在用手机刷抖音（{state}，"
                 f"第 {elapsed} 分钟，计划 {s['plan_min']} 分钟）。"]
        recent = s["events"][-3:]
        if recent:
            parts = []
            for e in reversed(recent):
                age = max(0, int((now - e["ts"]) / 60))
                desc = e["action"]
                if e["reason"]:
                    desc += f"（{e['reason']}）"
                parts.append(f"{age} 分钟前 {desc}" if age else f"刚刚 {desc}")
            lines.append("最近：" + "；".join(parts) + "。")
        if s["last_summary"]:
            lines.append(f"最近一条画面：{s['last_summary']}。")
        if s["last_error"]:
            lines.append(f"最近异常：{s['last_error']}。")
        if recent and now - recent[-1]["ts"] > 300:
            lines.append("最近 5 分钟没有新动作，可能卡住或手机不可用。")
        lines.append("用户问你在干嘛时，按这个状态用第一人称回答；"
                     "不要说自己没在刷，也不要说成另一个助手。"
                     "你只看到屏幕截图帧（没有声音），回答视频内容时措辞要诚实。")
        text = "\n".join(lines)
        if len(text) > _SELF_STATE_MAX:   # 评审约束：每轮注入 ≤200 字
            lines = [lines[0], lines[-1]]
            text = "\n".join(lines)
        return text[:_SELF_STATE_MAX]

    if s["ended_at"]:
        minutes = max(1, int((s["ended_at"] - s["started_at"]) / 60))
        tail = f"，最后一条：{s['last_summary']}" if s["last_summary"] else ""
        err = f"，期间出错：{s['last_error']}" if s["last_error"] else ""
        return (f"【你当前的状态】你刚结束了刷抖音（本次约 {minutes} 分钟{tail}{err}）。"
                "如果用户问，就说刚结束，不要继续说正在刷。")
    if s["last_error"]:
        return f"【你当前的状态】你上次刷抖音出错：{s['last_error']}。"
    return "【你当前的状态】你现在没有在刷抖音。"
