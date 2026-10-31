"""模型档案视图：QQ 侧切换模型用的只读工具（纯标准库，不依赖 nonebot/panel）。

档案的唯一写入方是控制台面板（data/providers.json）；本模块只负责
读取、名字匹配、可用性校验与 Key 掩码——Key 的录入永远在面板完成。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES_FILE = ROOT / "data" / "providers.json"
PROXY_FILE = ROOT / "data" / "proxy.json"


class ProfileError(ValueError):
    """档案不可用（无 Key/地址非法/Key 字段损坏），message 可直接发给用户。"""


def load_profiles() -> dict[str, dict]:
    """读全部模型档案；文件缺失/损坏返回空 dict（面板负责写入）。"""
    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mask(key: str) -> str:
    """Key 掩码：任何输出都只带掩码，绝不回显完整 Key。"""
    key = key or ""
    return key[:6] + "…" + key[-4:] if len(key) > 12 else ("…" if key else "(无)")


def match_name(profiles: dict[str, dict], query: str) -> str | None:
    """档案名匹配：精确优先，其次唯一前缀；歧义/未命中返回 None。"""
    q = (query or "").strip()
    if q in profiles:
        return q
    hits = [n for n in profiles if n.startswith(q)]
    return hits[0] if len(hits) == 1 else None


def is_local_base(base_url: str) -> bool:
    return (base_url or "").strip().startswith(("http://127.0.0.1", "http://localhost"))


def is_usable(profile: dict) -> bool:
    """快速判断档案是否可切换（列表展示用；严格校验见 validate）。"""
    base = (profile.get("base_url") or "").strip()
    key = (profile.get("api_key") or "").strip()
    return bool((profile.get("model") or "").strip()) and \
        (bool(key) or is_local_base(base))


def validate(profile: dict) -> None:
    """切换前严格校验：不合格抛 ProfileError，message 可直接给用户。"""
    base = (profile.get("base_url") or "").strip()
    key = (profile.get("api_key") or "").strip()
    if not base.startswith(("http://", "https://")):
        raise ProfileError("地址不合法")
    if not (profile.get("model") or "").strip():
        raise ProfileError("没有模型名")
    # Key 字段被污染成网址（历史数据事故）：绝不切换到坏档案
    if key.lower().startswith(("http://", "https://")):
        raise ProfileError("Key 字段存的是网址（档案数据损坏），请到面板重新保存")
    if not key and not is_local_base(base):
        raise ProfileError("没有 API Key，不可切换")


def proxy_active() -> bool:
    """反代是否处于激活状态（读面板写的 proxy.json，与面板互斥口径一致）。"""
    try:
        data = json.loads(PROXY_FILE.read_text(encoding="utf-8"))
        return bool(data.get("active"))
    except (OSError, ValueError):
        return False
