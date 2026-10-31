"""定时任务核心：任务定义、cron 解析、运行结果。

纯标准库，不 import nonebot / chat：面板进程和内核进程都可以安全导入。
任务定义（面板写、内核读）与运行结果（内核写、面板读）分文件存放，
避免两个进程同时读改写同一 JSON 导致丢更新。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "schedules.json"          # 任务定义
RUNTIME_FILE = ROOT / "data" / "schedule_runtime.json"  # 最近运行结果

MAX_TASKS = 50
MAX_CONTENT = 500
MAX_NAME_LEN = 30

# cron 五段式：分 时 日 月 周，每段支持 * / , - 数字
_FIELD_RE = re.compile(
    r"^(\*|(?:\d+|\*)(?:-\d+)?(?:/\d+)?)"
    r"(?:,(?:\d+|\*)(?:-\d+)?(?:/\d+)?)*$")
_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff-]")

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "tasks": None}
_rt_cache: dict = {"at": 0.0, "data": None}


# ---------- 任务定义 ----------

def _default_task() -> dict:
    return {"name": "", "cron": "", "content": "", "target": "",
            "type": "group", "enabled": True}


def _normalize(task: dict) -> dict:
    out = _default_task()
    for key in _default_task():
        if key in task:
            out[key] = task[key]
    out["name"] = str(out["name"])[:MAX_NAME_LEN]
    out["content"] = str(out["content"])[:MAX_CONTENT]
    out["cron"] = str(out["cron"]).strip()
    out["target"] = str(out["target"]).strip()
    out["type"] = "private" if str(out.get("type")) == "private" else "group"
    out["enabled"] = bool(out.get("enabled", True))
    return out


def clean_name(name: str) -> str:
    """任务名清洗：非法字符换 _，限长（add_task 与 QQ 指令侧共用）。"""
    return _NAME_RE.sub("_", (name or "").strip())[:MAX_NAME_LEN]


def minute_mark(now: float | None = None) -> int:
    """当前分钟的全局标记（本地时区）：到期判定与防重发共用，单一实现。"""
    return int(time.mktime(time.localtime(now))) // 60


def load_tasks() -> list[dict]:
    """读任务定义（2 秒缓存）。"""
    now = time.monotonic()
    if _cache["tasks"] is not None and now - _cache["at"] < 2.0:
        return _cache["tasks"]
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        tasks = data if isinstance(data, list) else (data.get("tasks") or [])
    except (OSError, ValueError):
        tasks = []
    cleaned = [_normalize(t) for t in tasks if isinstance(t, dict)]
    cleaned = [t for t in cleaned if t.get("name")][:MAX_TASKS]
    _cache.update(at=now, tasks=cleaned)
    return cleaned


def save_tasks(tasks: list[dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps([_normalize(t) for t in tasks],
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    _cache.update(at=time.monotonic(), tasks=None)


def _mutate(fn) -> list[dict]:
    """读-改-写定义文件；面板与内核各自进程内的线程安全。"""
    with _lock:
        tasks = list(load_tasks())
        result = fn(tasks)
        if result is not None:
            tasks = result
        save_tasks(tasks)
        return tasks


# ---------- 运行结果（内核写，面板读） ----------

def load_runtime() -> dict[str, dict]:
    now = time.monotonic()
    if _rt_cache["data"] is not None and now - _rt_cache["at"] < 1.0:
        return _rt_cache["data"]
    try:
        data = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    clean: dict[str, dict] = {}
    for name, item in data.items():
        if not isinstance(item, dict):
            continue
        clean[str(name)] = {
            "last_run": int(item.get("last_run") or 0),
            "last_ok": bool(item.get("last_ok")),
            "last_error": str(item.get("last_error") or "")[:120],
        }
    _rt_cache.update(at=now, data=clean)
    return clean


def save_runtime(data: dict[str, dict]) -> None:
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_FILE.with_suffix(RUNTIME_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(RUNTIME_FILE)
    _rt_cache.update(at=time.monotonic(), data=None)


def clear_runtime(name: str) -> None:
    with _lock:
        data = load_runtime()
        if name in data:
            data.pop(name, None)
            save_runtime(data)


def merged_tasks() -> list[dict]:
    """任务定义 + 最近运行结果，供面板/QQ 列表展示。"""
    runtime = load_runtime()
    out = []
    for task in load_tasks():
        item = dict(task)
        item.update(runtime.get(task["name"], {}))
        item["describe"] = describe_cron(task["cron"])
        out.append(item)
    return out


def due_tasks(now: float | None = None) -> list[dict]:
    """当前分钟应触发、且本分钟尚未触发过的任务（不补发停机期间的任务）。"""
    ts = time.localtime(now)
    mark = minute_mark(now)
    runtime = load_runtime()
    due = []
    for task in load_tasks():
        if not task["enabled"] or not task["content"] or not task["target"]:
            continue
        fields = cron_fields(task["cron"])
        if not fields or not cron_matches(fields, ts):
            continue
        if int(runtime.get(task["name"], {}).get("last_run") or 0) == mark:
            continue
        due.append(task)
    return due


def stamp_runtime(name: str, minute_mark: int, ok: bool, error: str = "") -> None:
    with _lock:
        data = load_runtime()
        data[name] = {
            "last_run": int(minute_mark),
            "last_ok": bool(ok),
            "last_error": "" if ok else (error or "发送失败")[:120],
        }
        save_runtime(data)


# ---------- 增删改 ----------

def _target_ok(target: str) -> bool:
    if not target or not target.lstrip("-").isdigit():
        return False
    return int(target) > 0


def add_task(name: str, cron: str, content: str, target: str,
             task_type: str = "group", enabled: bool = True) -> tuple[bool, str]:
    name = clean_name(name)
    cron = (cron or "").strip()
    content = (content or "").strip()
    target = (target or "").strip()
    task_type = "private" if task_type == "private" else "group"
    if not name:
        return False, "任务名不能为空"
    if not cron_valid(cron):
        return False, f"cron 格式不对：{describe_cron(cron)}"
    if not content:
        return False, "消息内容不能为空"
    if len(content) > MAX_CONTENT:
        return False, f"内容太长（最多 {MAX_CONTENT} 字）"
    if not _target_ok(target):
        return False, "目标号码不对：群号/QQ号只能是数字"
    state = {"ok": True}

    def apply(tasks: list[dict]) -> list[dict]:
        others = [t for t in tasks if t["name"] != name]
        if len(others) >= MAX_TASKS:
            state["ok"] = False
            return tasks
        return others + [_normalize({
            "name": name, "cron": cron, "content": content,
            "target": target, "type": task_type, "enabled": enabled})]

    _mutate(apply)
    if not state["ok"]:
        return False, f"任务太多啦（最多 {MAX_TASKS} 个），先删几个"
    return True, f"已保存「{name}」：{describe_cron(cron)} → {target}"


def update_task(name: str, **fields) -> tuple[bool, str]:
    found = {"ok": False}

    def apply(tasks: list[dict]) -> list[dict]:
        out = []
        for task in tasks:
            if task["name"] != name:
                out.append(task)
                continue
            found["ok"] = True
            merged = dict(task)
            for key in ("cron", "content", "target", "type", "enabled"):
                if key in fields and fields[key] is not None:
                    merged[key] = fields[key]
            if not cron_valid(str(merged["cron"])):
                raise ValueError(f"cron 格式不对：{describe_cron(str(merged['cron']))}")
            if not _target_ok(str(merged["target"])):
                raise ValueError("目标号码不对：群号/QQ号只能是数字")
            out.append(_normalize(merged))
        return out

    try:
        _mutate(apply)
    except ValueError as exc:
        return False, str(exc)
    if not found["ok"]:
        return False, f"没有叫「{name}」的任务"
    return True, f"已更新「{name}」"


def remove_task(name: str) -> tuple[bool, str]:
    found = {"ok": False}

    def apply(tasks: list[dict]) -> list[dict]:
        rest = [t for t in tasks if t["name"] != name]
        found["ok"] = len(rest) != len(tasks)
        return rest

    _mutate(apply)
    if not found["ok"]:
        return False, f"没有叫「{name}」的任务"
    clear_runtime(name)   # 运行结果一并清掉，不留永久残留（clear_runtime 由此复活）
    return True, f"已删除「{name}」"


def set_enabled(name: str, enabled: bool) -> tuple[bool, str]:
    ok, msg = update_task(name, enabled=enabled)
    if ok:
        return True, f"「{name}」已{'开启' if enabled else '关闭'}"
    return ok, msg


def list_text() -> str:
    tasks = merged_tasks()
    if not tasks:
        return "定时任务列表是空的。用法：定时任务 添加 <名字> | <cron> | <内容> | <群号>"
    lines = [f"⏰ 定时任务（{len(tasks)}）："]
    for task in tasks:
        flag = "开" if task["enabled"] else "关"
        where = "好友" if task["type"] == "private" else "群"
        lines.append(f"· [{flag}] {task['name']}｜{task['describe']}"
                     f"｜{where}{task['target']}｜{task['content'][:20]}")
    return "\n".join(lines)


# ---------- cron ----------

def _parse_field(text: str, low: int, high: int) -> set[int] | None:
    text = text.strip()
    if not text or not _FIELD_RE.match(text):
        return None
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        body = part
        has_step = "/" in part
        if has_step:
            body, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1 or int(step_text) > 1000:
                return None
            step = int(step_text)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            lo, _, hi = body.partition("-")
            if not (lo.isdigit() and hi.isdigit()):
                return None
            start, end = int(lo), int(hi)
        elif body.isdigit():
            start = int(body)
            # 标准 cron 的 5/10 = 5,15,25…；5/1 = 5,6,7…；只有纯 5 才是单值
            end = high if has_step else start
        else:
            return None
        if start > end or start < low or end > high:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def cron_fields(cron: str) -> dict[str, set[int]] | None:
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow_raw = _parse_field(parts[4], 0, 7)
    if None in (minute, hour, dom, month, dow_raw):
        return None
    dow = {d % 7 for d in dow_raw}
    return {"minute": minute, "hour": hour, "dom": dom, "month": month, "dow": dow}


def cron_valid(cron: str) -> bool:
    return cron_fields(cron) is not None


def cron_matches(fields: dict[str, set[int]], tm: time.struct_time) -> bool:
    cron_dow = (tm.tm_wday + 1) % 7
    dom_ok = tm.tm_mday in fields["dom"]
    dow_ok = cron_dow in fields["dow"]
    dom_restricted = fields["dom"] != set(range(1, 32))
    dow_restricted = fields["dow"] != set(range(7))
    # 标准 cron：日/周都受限时取 OR；只受限一个时取对应判断
    if dom_restricted and dow_restricted:
        date_ok = dom_ok or dow_ok
    else:
        date_ok = dom_ok and dow_ok
    return (tm.tm_min in fields["minute"]
            and tm.tm_hour in fields["hour"]
            and tm.tm_mon in fields["month"]
            and date_ok)


# 兼容旧名（测试/老代码）
_cron_matches = cron_matches


def _step_of(values: set[int]) -> int | None:
    if len(values) < 2:
        return None
    ordered = sorted(values)
    step = ordered[1] - ordered[0]
    if step < 1:
        return None
    if ordered != list(range(ordered[0], ordered[0] + step * len(ordered), step)):
        return None
    return step


def describe_cron(cron: str) -> str:
    fields = cron_fields(cron)
    if not fields:
        return "cron 格式不对：需要 5 段（分 时 日 月 周），如 0 9 * * 1-5"
    minute, hour, dom, month, dow = (fields["minute"], fields["hour"],
                                     fields["dom"], fields["month"], fields["dow"])
    if minute == set(range(60)) and hour == set(range(24)):
        base = "每分钟"
    elif len(hour) == 1 and len(minute) == 1:
        base = f"{next(iter(hour)):02d}:{next(iter(minute)):02d}"
    elif hour == set(range(24)) and _step_of(minute):
        base = f"每 {_step_of(minute)} 分钟"
    elif len(hour) == 1 and _step_of(minute):
        base = f"每天 {next(iter(hour))} 点，每 {_step_of(minute)} 分钟"
    elif len(hour) == 1 and minute == set(range(60)):
        base = f"每天 {next(iter(hour))} 点的每分钟"
    elif len(minute) == 1 and len(hour) > 1:
        base = f"每天 {next(iter(minute))} 分（{sorted(hour)} 点）"
    else:
        base = f"分钟 {sorted(minute)[:5]}、小时 {sorted(hour)[:5]}"

    if dow != set(range(7)):
        names = "日一二三四五六"
        picked = "".join(names[d] for d in sorted(dow, key=lambda d: (d - 1) % 7))
        if dom == set(range(1, 32)):
            base = f"每周{picked} {base}" if base != "每分钟" else f"每周{picked}每分钟"
        else:
            base += f"（周{picked}）"
    elif dom != set(range(1, 32)):
        base += f"（每月 {','.join(str(d) for d in sorted(dom)[:5])} 号）"
    if month != set(range(1, 13)):
        base += f"（{','.join(str(m) for m in sorted(month))} 月）"
    if (dow == set(range(7)) and dom == set(range(1, 32))
            and month == set(range(1, 13))
            and not base.startswith(("每", "每天"))):
        base = "每天 " + base
    return base
