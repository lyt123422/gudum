"""定时任务插件：面板配 cron + 内容，机器人到点自动往指定群/好友发消息。

给谁用：管理员（增删改查走 QQ 指令，面板可视化配置）。
触发：私聊或群里 @机器人 发「定时任务 / 定时任务 添加 … / 定时任务 删除 <名> /
      定时任务 列表 / 定时任务 开关 <名>」。
面板：控制台「⏰ 定时任务」卡片，增删改查走 /api/schedule/*，前端 F5 即生效。

调度模型：单后台 asyncio 任务 + 1 秒 tick，按 cron 五段式（分 时 日 月 周）匹配。
不引入 apscheduler 等第三方依赖——避免与市场里的 nonebot-plugin-apscheduler 抢
事件循环，也省一次内核重启。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time

from nonebot import get_bot, get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from .. import plugin_manager
from ..envutil import ROOT
from .chat import _cmd_gate, _is_admin

STATE_FILE = ROOT / "data" / "schedules.json"

MAX_TASKS = 50          # 防面板被刷爆，也防 tick 每轮扫太多
MAX_CONTENT = 500       # 单条消息长度上限（QQ 长消息会被风控）
MAX_NAME_LEN = 30

def _cfg(name: str, default: str) -> str:
    """配置读取：优先 nonebot 已加载的 .env（键转小写），兜底进程环境变量。"""
    v = getattr(get_driver().config, name.lower(), None)
    if v is None or v == "":
        v = os.getenv(name)
    return str(v) if v not in (None, "") else default


# 关了就完全不调度（面板仍可编辑任务，但不发送）
SCHEDULE_ENABLED = _cfg("SCHEDULE_ENABLED", "true").lower() in ("true", "1", "yes")
try:
    TICK_SECONDS = max(0.2, float(_cfg("SCHEDULE_TICK", "1")))
except ValueError:
    TICK_SECONDS = 1.0

# cron 五段式：分 时 日 月 周，每段支持 * / , - 数字
_FIELD_RE = re.compile(r"^(\*|(?:\d+|\*)(?:-\d+)?(?:/\d+)?)(?:,(?:\d+|\*)(?:-\d+)?(?:/\d+)?)*$")
_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff-]")

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "tasks": None}
_worker: asyncio.Task | None = None


# ---------- 存取 ----------

def _default_task() -> dict:
    return {"name": "", "cron": "", "content": "", "target": "",
            "type": "group", "enabled": True}


def _load() -> list[dict]:
    """读任务列表（带 2 秒缓存，和 plugin_manager 同款节奏）。"""
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


def _normalize(task: dict) -> dict:
    out = _default_task()
    # 运行期结果字段不进 _default_task（否则每个新任务都带一堆空键），单独保留
    out["last_run"] = int(task.get("last_run") or 0)
    out["last_ok"] = bool(task.get("last_ok"))
    out["last_error"] = str(task.get("last_error") or "")[:120]
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


def _save(tasks: list[dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps([_normalize(t) for t in tasks],
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    _cache.update(at=time.monotonic(), tasks=None)


def _mutate(fn) -> list[dict]:
    """读-改-写串行化：面板线程与 QQ 指令协程可能同时写。"""
    with _lock:
        tasks = list(_load())
        result = fn(tasks)
        if result is not None:
            tasks = result
        _save(tasks)
        _cache.update(at=time.monotonic(), tasks=None)
        return tasks


# ---------- cron ----------

def _parse_field(text: str, low: int, high: int) -> set[int] | None:
    """解析单个 cron 段；非法返回 None。步长上限 1000，防 /0 死循环。"""
    text = text.strip()
    if not text or not _FIELD_RE.match(text):
        return None
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        body = part
        if "/" in part:
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
            start = end = int(body)
        else:
            return None
        if start > end or start < low or end > high:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def cron_fields(cron: str) -> dict[str, set[int]] | None:
    """解析 "分 时 日 月 周"；任一段非法或段数不对返回 None。"""
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    # 周：cron 惯例 0=周日；也兼容 7=周日
    dow_raw = _parse_field(parts[4], 0, 7)
    if None in (minute, hour, dom, month, dow_raw):
        return None
    dow = {d % 7 for d in dow_raw}
    return {"minute": minute, "hour": hour, "dom": dom, "month": month, "dow": dow}


def cron_valid(cron: str) -> bool:
    return cron_fields(cron) is not None


def _cron_matches(fields: dict[str, set[int]], tm: time.struct_time) -> bool:
    # tm_wyday: 0=周一..6=周日；转成 cron 的 0=周日
    cron_dow = (tm.tm_wday + 1) % 7
    return (tm.tm_min in fields["minute"]
            and tm.tm_hour in fields["hour"]
            and tm.tm_mday in fields["dom"]
            and tm.tm_mon in fields["month"]
            and cron_dow in fields["dow"])


def _step_of(values: set[int]) -> int | None:
    """分钟/小时集合是不是「每 N」的等步长序列（0,5,10… → 5）。"""
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
    """给面板和 QQ 用的中文说明；非法表达式返回原因。"""
    fields = cron_fields(cron)
    if not fields:
        return "cron 格式不对：需要 5 段（分 时 日 月 周），如 0 9 * * 1-5"
    minute, hour, dom, month, dow = (fields["minute"], fields["hour"],
                                     fields["dom"], fields["month"], fields["dow"])
    if minute == set(range(60)) and hour == set(range(24)):
        base = "每分钟"
    elif len(hour) == 1 and len(minute) == 1:
        base = f"每天 {next(iter(hour)):02d}:{next(iter(minute)):02d}"
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
        base += f"（周{picked}）"
    elif dom != set(range(1, 32)):
        base += f"（每月 {','.join(str(d) for d in sorted(dom)[:5])} 号）"
    if month != set(range(1, 13)):
        base += f"（{','.join(str(m) for m in sorted(month))} 月）"
    return base


# ---------- 到期判定 ----------

def _due_tasks(now: float | None = None) -> list[dict]:
    """返回当前这一分钟应该触发、且尚未触发过的任务。

    last_run 记的是"上次触发的分钟数"，同一分钟内重复 tick 不会重发（重启后
    也从当前分钟续上，不会把停机期间错过的任务一次性补发——那是刷屏事故）。
    """
    if not SCHEDULE_ENABLED:
        return []
    ts = time.localtime(now)
    minute_mark = int(time.mktime(ts)) // 60
    due = []
    for task in _load():
        if not task["enabled"] or not task["content"] or not task["target"]:
            continue
        fields = cron_fields(task["cron"])
        if not fields or not _cron_matches(fields, ts):
            continue
        if int(task.get("last_run") or 0) == minute_mark:
            continue
        due.append(task)
    return due


async def _send(task: dict) -> bool:
    """按任务目标发消息；失败返回 False（风控/目标不存在都不算致命）。"""
    try:
        bot = get_bot()
    except Exception:
        logger.warning("定时任务「{}」：机器人未连接，跳过", task["name"])
        return False
    target = str(task["target"])
    try:
        if task["type"] == "private":
            await bot.send_private_msg(user_id=int(target), message=task["content"])
        else:
            await bot.send_group_msg(group_id=int(target), message=task["content"])
    except Exception as exc:
        logger.warning("定时任务「{}」发送失败：{}", task["name"], exc)
        return False
    return True


async def _tick_once(now: float | None = None) -> int:
    """一轮调度；返回成功发出的条数。"""
    sent = 0
    for task in _due_tasks(now):
        ok = await _send(task)
        minute_mark = int(time.mktime(time.localtime(now))) // 60
        _mutate(_stamp(task["name"], minute_mark, ok))
        sent += 1 if ok else 0
    return sent


def _stamp(name: str, minute_mark: int, ok: bool):
    """记录本次触发结果（默认参数绑定，避免闭包捕获循环变量）。"""

    def apply(tasks: list[dict]) -> list[dict]:
        return [{**t, "last_run": minute_mark, "last_ok": ok,
                 "last_error": "" if ok else "发送失败"} if t["name"] == name else t
                for t in tasks]

    return apply


async def _loop() -> None:
    while True:
        try:
            await _tick_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:      # 单轮异常不能打死整个调度器
            logger.warning("定时任务调度异常：{}", exc)
        await asyncio.sleep(TICK_SECONDS)


def start_worker() -> None:
    """启动调度协程；重复调用安全。"""
    global _worker
    if _worker is not None and not _worker.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _worker = loop.create_task(_loop())


def stop_worker() -> None:
    global _worker
    if _worker is not None and not _worker.done():
        _worker.cancel()
    _worker = None


def plugin_status() -> dict:
    """面板用：任务列表 + 调度器是否在跑。"""
    return {"tasks": [_public(t) for t in _load()],
            "running": _worker is not None and not _worker.done(),
            "max_tasks": MAX_TASKS}


def _public(task: dict) -> dict:
    return {"name": task["name"], "cron": task["cron"], "content": task["content"],
            "target": task["target"], "type": task["type"],
            "enabled": task["enabled"], "describe": describe_cron(task["cron"]),
            "last_ok": bool(task.get("last_ok")),
            "last_error": str(task.get("last_error") or "")[:120]}


# ---------- 增删改（面板与 QQ 指令共用，返回 (ok, 消息)） ----------

def add_task(name: str, cron: str, content: str, target: str,
             task_type: str = "group", enabled: bool = True) -> tuple[bool, str]:
    name = _NAME_RE.sub("_", (name or "").strip())[:MAX_NAME_LEN]
    cron = (cron or "").strip()
    content = (content or "").strip()
    target = (target or "").strip()
    if not name:
        return False, "任务名不能为空"
    if not cron_valid(cron):
        return False, f"cron 格式不对：{describe_cron(cron)}"
    if not content:
        return False, "消息内容不能为空"
    if len(content) > MAX_CONTENT:
        return False, f"内容太长（最多 {MAX_CONTENT} 字）"
    if not _target_ok(target, task_type):
        return False, "目标号码不对：群号/QQ号只能是数字"
    state = {"ok": True}

    def apply(tasks: list[dict]) -> list[dict]:
        if len([t for t in tasks if t["name"] != name]) >= MAX_TASKS:
            state["ok"] = False
            return tasks
        rest = [t for t in tasks if t["name"] != name]
        return rest + [_normalize({"name": name, "cron": cron, "content": content,
                                   "target": target, "type": task_type,
                                   "enabled": enabled})]

    _mutate(apply)
    if not state["ok"]:
        return False, f"任务太多啦（最多 {MAX_TASKS} 个），先删几个"
    return True, f"已保存「{name}」：{describe_cron(cron)} → {target}"


def update_task(name: str, **fields) -> tuple[bool, str]:
    found = {"ok": False}

    def apply(tasks: list[dict]) -> list[dict]:
        out = []
        for t in tasks:
            if t["name"] != name:
                out.append(t)
                continue
            found["ok"] = True
            merged = dict(t)
            for key in ("cron", "content", "target", "type", "enabled"):
                if key in fields and fields[key] is not None:
                    merged[key] = fields[key]
            if not cron_valid(str(merged["cron"])):
                raise ValueError(f"cron 格式不对：{describe_cron(str(merged['cron']))}")
            if not _target_ok(str(merged["target"]), str(merged["type"])):
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
    return (True, f"已删除「{name}」") if found["ok"] else (False, f"没有叫「{name}」的任务")


def set_enabled(name: str, enabled: bool) -> tuple[bool, str]:
    ok, msg = update_task(name, enabled=enabled)
    if ok:
        return True, f"「{name}」已{'开启' if enabled else '关闭'}"
    return ok, msg


def _target_ok(target: str, task_type: str) -> bool:
    if not target or not target.lstrip("-").isdigit():
        return False
    # 群号/QQ号都是正整数；好友号理论上也 >10000，但不做强校验
    return int(target) > 0


def list_text() -> str:
    tasks = _load()
    if not tasks:
        return "定时任务列表是空的。用法：定时任务 添加 <名字> | <cron> | <内容> | <群号>"
    lines = [f"⏰ 定时任务（{len(tasks)}）："]
    for t in tasks:
        flag = "开" if t["enabled"] else "关"
        where = "好友" if t["type"] == "private" else "群"
        lines.append(f"· [{flag}] {t['name']}｜{describe_cron(t['cron'])}"
                     f"｜{where}{t['target']}｜{t['content'][:20]}")
    return "\n".join(lines)


# ---------- QQ 指令 ----------

cmd_sched = on_command("定时任务", rule=_cmd_gate, priority=4, block=True)


async def _enabled(matcher) -> bool:
    if plugin_manager.is_enabled("schedule"):
        return True
    await matcher.finish("定时任务插件没开启：控制台 → 🧩 插件 里可以打开")
    return False


@cmd_sched.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _enabled(matcher)
    if not _is_admin(event.user_id):
        await matcher.finish("定时任务只有管理员能设置哦")
    text = (args.extract_plain_text() or "").strip()
    if not text or text in ("列表", "list"):
        await matcher.finish(list_text())
    head, _, rest = text.partition(" ")
    head, rest = head.strip(), rest.strip()
    if head in ("添加", "新增", "add"):
        await matcher.finish(_cmd_add(rest, event))
    if head in ("删除", "移除", "del"):
        if not rest:
            await matcher.finish("用法：定时任务 删除 <名字>")
        ok, msg = remove_task(rest)
        await matcher.finish(msg)
    if head in ("开", "开启", "on"):
        ok, msg = set_enabled(rest, True)
        await matcher.finish(msg)
    if head in ("关", "关闭", "off"):
        ok, msg = set_enabled(rest, False)
        await matcher.finish(msg)
    if head in ("立即", "测试", "run"):
        await matcher.finish(await _cmd_run(rest))
    await matcher.finish(
        "用法：\n定时任务 列表\n定时任务 添加 <名字> | <cron> | <内容> | <群号>\n"
        "定时任务 删除/开启/关闭 <名字>\n定时任务 立即 <名字>\n"
        "cron 例：0 9 * * 1-5 = 工作日 9:00")


def _cmd_add(rest: str, event: MessageEvent) -> str:
    parts = [p.strip() for p in rest.split("|")]
    if len(parts) < 4:
        return ("用法：定时任务 添加 <名字> | <cron> | <内容> | <群号>\n"
                "例：定时任务 添加 早报 | 0 9 * * 1-5 | 大家早！ | 123456\n"
                "发给好友用：定时任务 添加 提醒 | 30 8 * * * | 该起床了 | friend:10001")
    name, cron, content = parts[0], parts[1], parts[2]
    target_raw = parts[3]
    if target_raw.lower().startswith("friend:"):
        task_type, target = "private", target_raw.split(":", 1)[1].strip()
    else:
        # 没写前缀时按当前会话推断：私聊里配置默认发给自己
        task_type = "private" if event.message_type == "private" else "group"
        target = target_raw
    ok, msg = add_task(name, cron, content, target, task_type)
    return msg


async def _cmd_run(name: str) -> str:
    """手动立即触发一次（不含 cron 判定），用于配完先验证能不能发出去。"""
    if not name:
        return "用法：定时任务 立即 <名字>"
    task = next((t for t in _load() if t["name"] == name), None)
    if task is None:
        return f"没有叫「{name}」的任务"
    ok = await _send(task)
    return f"「{name}」已发出" if ok else (
        f"「{name}」发送失败（机器人没连上，或号码不对/被风控）")


def _boot() -> None:
    """内核加载本插件时启动调度器。"""
    try:
        if SCHEDULE_ENABLED and plugin_manager.is_enabled("schedule"):
            start_worker()
    except Exception as exc:
        logger.warning("定时任务调度器启动失败：{}", exc)


_boot()
