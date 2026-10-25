"""角色语音：edge-tts（微软 Edge 朗读接口，免费无 Key，异步，零模型下载）。

音色示例：zh-CN-XiaoxiaoNeural(女·晓晓) zh-CN-YunxiNeural(男·云希)
         zh-CN-XiaoyiNeural(女·晓伊)  zh-TW-HsiaoChenNeural(女·台湾)
完整列表: https://edge-tts.netlify.app (或 edge-tts --list-voices)
"""
from __future__ import annotations

import time
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "data" / "voice"


async def synth(text: str, voice: str = "zh-CN-XiaoxiaoNeural") -> Path:
    """文本合成语音，返回 mp3 文件路径；失败抛异常由调用方静默降级。"""
    text = text.strip()[:300]   # TTS 太长既慢又容易被平台限制
    communicate = edge_tts.Communicate(text, voice)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    if not buf:
        raise RuntimeError("edge-tts 未返回音频")
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    path = VOICE_DIR / f"{int(time.time() * 1000)}.mp3"
    path.write_bytes(bytes(buf))
    # 只保留最近 20 条，语音是即发即弃的中间产物
    for stale in sorted(VOICE_DIR.glob("*.mp3"))[:-20]:
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    return path
