"""DeepSeek API 客户端（OpenAI 兼容接口）。

无状态：会话记忆由插件层管理，这里只负责一次调用和错误翻译——
所有异常都转成 DeepSeekError，message 可以直接发给 QQ 用户。
"""
from __future__ import annotations

import openai


class DeepSeekError(Exception):
    """面向用户的错误：message 为可直接展示的中文提示，raw_detail 仅供日志。"""

    def __init__(self, message: str, raw_detail: str = ""):
        super().__init__(message)
        self.message = message
        self.raw_detail = raw_detail


def _reasoning_tokens(resp) -> int:
    """取推理 token 数；服务商没暴露该字段时返回 0（不影响主流程）。"""
    try:
        details = resp.usage.completion_tokens_details
        return int(getattr(details, "reasoning_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


class DeepSeekClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 60.0, max_retries: int = 2,
                 temperature: float = 0.8, max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = base_url or "https://api.deepseek.com"
        # 空 Key 时填占位值：让请求走出去拿 401，错误信息比构造时报错更友好
        self._client = openai.AsyncOpenAI(
            base_url=base_url or "https://api.deepseek.com",
            api_key=api_key or "EMPTY",
            timeout=timeout,
            max_retries=max_retries,
        )

    async def aclose(self) -> None:
        """释放连接池（评审 v3 P1-5：每次决策新建的客户端必须关，否则泄漏）。"""
        await self._client.close()

    async def _create(self, messages: list[dict], temperature: float | None,
                      max_tokens: int):
        return await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=self.temperature if temperature is None else temperature,
            stream=False,
        )

    async def chat(self, messages: list[dict],
                   temperature: float | None = None) -> str:
        """messages: [{"role": "system"|"user"|"assistant", "content": str|list}]

        推理模型（gemini-3.x-flash-high、deepseek-reasoner 等）会把思考 token 算进
        max_tokens：思考吃满时正文为空、finish_reason=length。这种情况不是模型拒答，
        是预算不够——放宽一档重试一次，仍空才报错。
        """
        budget = self.max_tokens
        try:
            resp = await self._create(messages, temperature, budget)
        except openai.AuthenticationError as exc:
            raise DeepSeekError(
                "API Key 无效：请检查 .env 里的 DEEPSEEK_API_KEY 是否填对",
                str(exc)) from exc
        except openai.RateLimitError as exc:
            raise DeepSeekError("请求太频繁啦，稍等几秒再试～", str(exc)) from exc
        except openai.APITimeoutError as exc:
            raise DeepSeekError("DeepSeek 响应超时了，再发一次试试", str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise DeepSeekError("连不上 DeepSeek 服务，请检查本机网络", str(exc)) from exc
        except openai.BadRequestError as exc:
            detail = str(exc)
            lowered = detail.lower()
            if any(k in lowered for k in ("image", "multimodal", "vision", "图片")):
                raise DeepSeekError(
                    "当前模型不支持看图：在控制台换多模态模型"
                    "（gemini / gpt-4o / qwen-vl / glm-4v 系列）", detail) from exc
            raise DeepSeekError(
                "请求被模型拒绝（上下文可能太长，发「重置」清空记忆再试）",
                detail[:300]) from exc
        except openai.APIStatusError as exc:
            # 402 是 DeepSeek 特有的计费语义：账户余额不足
            if exc.status_code == 402:
                raise DeepSeekError(
                    "DeepSeek 账户余额不足，请到 platform.deepseek.com 充值",
                    str(exc)) from exc
            raise DeepSeekError(
                f"DeepSeek 返回错误（HTTP {exc.status_code}）", str(exc)[:300]) from exc
        except Exception as exc:
            raise DeepSeekError("AI 服务出了点小问题，请稍后再试", str(exc)[:300]) from exc

        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        if not text:
            # 空正文 + 思考吃满预算 → 翻倍重试一次（只重试这一种，别把真拒答也重试）
            tight = (not resp.choices
                     or resp.choices[0].finish_reason == "length"
                     or bool(_reasoning_tokens(resp)))
            if tight:
                try:
                    resp = await self._create(messages, temperature, min(budget * 4, 16384))
                    text = (resp.choices[0].message.content or "").strip() \
                        if resp.choices else ""
                except Exception as exc:
                    raise DeepSeekError(
                        "AI 服务出了点小问题，请稍后再试", str(exc)[:300]) from exc
        if not text:
            raise DeepSeekError("AI 没有返回内容，请换个问法试试")
        return text
