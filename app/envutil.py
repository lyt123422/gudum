"""极简 .env 读取/原位更新，供独立小工具与内核面板共用。

机器人本体的启动配置走 nonebot 自带的 dotenv；这里服务运行期改写场景
（控制台保存、管理员认领、账号采用）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvValueError(ValueError):
    """.env 值非法（换行注入/键名非法），调用方应转为 400 提示。"""


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # SUPERUSERS=["123"] 这类 JSON 值外层没有引号，只有纯引号包裹才剥掉
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def update_env(path: Path, values: dict[str, str]) -> None:
    """原位更新 .env：只替换目标键所在行，注释与其他配置保留；原子写。

    值里禁止换行/回车/NUL——否则一个带 \\n 的字段就能注入任意 .env 键
    （评审 P1-5）。
    """
    for key, val in values.items():
        if not _ENV_KEY_RE.match(key):
            raise EnvValueError(f"非法的配置键名: {key!r}")
        if any(ch in str(val) for ch in ("\r", "\n", "\0")):
            raise EnvValueError(f"配置值不能包含换行或空字符: {key}")
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in values.items():
        if key not in seen:
            out.append(f"{key}={val}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
