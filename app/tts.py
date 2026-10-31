"""角色语音：edge-tts（免费预置音色）+ 可选 GPT-SoVITS 本地声音克隆后端。

- 默认（TTS_PROVIDER 为空）：edge-tts 预置音色；音色 = 人设 "# 语音:" 行 或 TTS_VOICE
- TTS_PROVIDER=gptsovits：「唯一音色」模式——所有语音统一走本地 GPT-SoVITS
  克隆音色（人设的 "# 语音:" 在此模式下被忽略）；克隆服务不可用时自动降级
  edge-tts，保证机器人不哑。

.env 配置（克隆模式）：
    TTS_PROVIDER=gptsovits
    GSV_API=http://127.0.0.1:9880
    GSV_REF_AUDIO=C:\\path\\to\\ref.mp3      # 参考音频（≥10s 干净人声）
    GSV_PROMPT_TEXT=参考音频里说的那句话原文   # 音频+文字配对是克隆的钥匙
    GSV_SPEED=1.0                            # 可选，语速
"""
from __future__ import annotations

import time
from pathlib import Path

import edge_tts
import httpx
from nonebot.log import logger

from .envutil import ENV_FILE, load_env

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "data" / "voice"


def _save_audio(data: bytes, suffix: str) -> Path:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    path = VOICE_DIR / f"{int(time.time() * 1000)}{suffix}"
    path.write_bytes(data)
    # 只保留最近 20 条，语音是即发即弃的中间产物
    for stale in sorted(VOICE_DIR.glob("*.mp3"))[:-20]:
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    return path


async def _synth_gptsovits(text: str) -> Path:
    """调本地 GPT-SoVITS API（api_v2，GET /tts）合成克隆音色，返回 wav 路径。"""
    env = load_env(ENV_FILE)
    api = (env.get("GSV_API") or "http://127.0.0.1:9880").rstrip("/")
    ref = (env.get("GSV_REF_AUDIO") or "").strip()
    prompt = (env.get("GSV_PROMPT_TEXT") or "").strip()
    if not ref or not Path(ref).exists():   # noqa: ASYNC240 单次 stat 微秒级
        raise FileNotFoundError("GSV_REF_AUDIO 未配置或参考音频文件不存在")
    params = {
        "text": text,
        "text_lang": env.get("GSV_TEXT_LANG") or "zh",
        "ref_audio_path": ref,
        "prompt_text": prompt,
        "prompt_lang": env.get("GSV_PROMPT_LANG") or "zh",
        "speed_factor": float(env.get("GSV_SPEED") or "1.0"),
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{api}/tts", params=params)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "audio" not in ctype and "octet-stream" not in ctype:
            raise RuntimeError(f"GSV 返回非音频: {resp.text[:120]}")
        return _save_audio(resp.content, ".wav")


async def _synth_edge(text: str, voice: str) -> Path:
    communicate = edge_tts.Communicate(text, voice)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    if not buf:
        raise RuntimeError("edge-tts 未返回音频")
    return _save_audio(bytes(buf), ".mp3")


async def synth(text: str, voice: str = "zh-CN-XiaoxiaoNeural") -> Path:
    """文本合成语音，返回音频文件路径；失败抛异常由调用方静默降级。

    TTS_PROVIDER=gptsovits 时统一走克隆音色（唯一音色模式，忽略人设音色）；
    克隆服务不可用时自动降级 edge-tts 并记日志。
    """
    text = text.strip()[:600]   # 太长既慢又容易被平台限制
    env = load_env(ENV_FILE)
    if (env.get("TTS_PROVIDER") or "").strip().lower() == "gptsovits":
        try:
            return await _synth_gptsovits(text)
        except Exception as exc:   # noqa: BLE001 降级：克隆服务挂了也不能哑
            logger.warning("GPT-SoVITS 克隆语音失败，降级 edge-tts: {}",
                           str(exc)[:120])
    return await _synth_edge(text, voice)
