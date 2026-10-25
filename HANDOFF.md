# gudum（deepseek-qq-bot）代码评审交接 v2

> 代码快照：2026-10-25 晚（v2：插件体系/抖音/看图等新功能后）
> 项目路径：`C:\Users\john\.zcode\workspace\default\deepseek-qq-bot`
> 技术栈：Python 3.13 · NoneBot2 + OneBot v11 · FastAPI 本地面板 · NapCat 协议端 · DrissionPage（浏览器自动化）
> 用途：交给评审 AI 做全面检验。自包含，不需要原对话上下文。行号可能漂移，以实际代码为准。

## 0. 给评审 AI

请先通读本文和 `app/` 全部源码，重点审 v2 新增部分（第 3 节），输出：问题清单（按 P0-P3 分级）+
具体修复建议。只读评审：不要改代码、不要动 `napcat/` 和 `.env`、不要真开浏览器刷抖音。
上一轮评审（CSRF/XSS/注入/脱敏等）已修复并验证，可抽查但不必重查。

## 1. 项目概述

开源免费的本地 QQ AI 聊天搭子：用户填任意 OpenAI 兼容模型 API + QQ 小号扫码登录，
私聊直聊/群@/发图聊天，多人设库（称呼/开场白/示例对话/防越狱/专属音色）、会话记忆、
本地 Web 控制台（模型提供商分档档案/人设管理/插件市场/运行控制/日志）、可选插件（联网搜索/
角色语音/表情包/刷抖音遥控）。初心是开源给不会搭建的小白用。

## 2. 架构（三进程 + 一个可选浏览器）

```
控制台 panel   127.0.0.1:8090  app/panel.py + panel_page.html（侧边栏六页）
机器人内核     127.0.0.1:8081  NoneBot2(app/main.py) + 内置插件(app/plugins/*)
QQ协议端 NapCat                app/napcat_runner.py 拉起，反向 WS 连内核
抖音浏览器（可选）             DrissionPage 接管 Chrome，data/douyin_profile 存登录态
```

## 3. v2 新增（本轮评审重点）

### 3.1 插件体系（`app/plugin_manager.py` + 面板「🧩 插件」页）
- 内置插件（websearch/voice/memes/douyin）：`data/plugins.json` 开关，2 秒缓存，运行时生效
- 第三方市场：pip 白名单（`local:` 前缀=本地插件，安装=装依赖+开启；其余 pip 安装+重启内核），
  失败自动换清华镜像重试
- 自测过：白名单拒绝任意包名；本地插件安装实测成功

### 3.2 抖音遥控（`app/douyin.py` + `app/plugins/douyin.py`）——重点审
- 线程模型：浏览器生命周期用 `browser_lock` 串行；自动刷是后台线程；watchdog 空闲 20 分钟关浏览器；
  所有 QQ 指令经 `asyncio.to_thread` 进线程池
- 行为库：随机停留/滑动 + 8% 翻评论区 + 5% 进作者主页 + 10% 回看 + 概率点赞
- 选择器：多候选 + JS 递归穿透 shadow DOM（抖音播放器是 web-component）——**未在真实登录态验证过**
- 限幅：单次 ≤60 分钟、每日 ≤120 分钟、仅管理员、评论仅人工触发
- 建议重点审：跨线程共享 DrissionPage page 对象是否安全、_act/_ensure_page 的锁序、
  page.back() 离开推荐流后的恢复、异常后 page 对象失效的处理

### 3.3 看图（`app/plugins/chat.py` `_extract_images` + `_do_chat` images 参数）
- 发图按 OpenAI vision 格式（image_url 内容块）发给模型；QQ 图片 URL 是多媒体域名签名 URL
- **已知疑点请评审**：QQ 图片 URL（multimedia.nt.qq.com.cn，带签名有时效）由模型服务端拉取，
  换了部署位置/过期的 URL 是否会 403？是否应该下载后转 base64 上传？
- 记忆只存文字（q 字符串），图不进记忆——请确认该取舍是否合理

### 3.4 其他 v2 功能（常规审即可）
- 角色语音（`app/tts.py` edge-tts，按用户开关 `data/voice_users.json`，人设 `# 语音:` 元信息）
- 表情包（`app/plugins/memes.py` 回复图片消息收录，本地库）
- 用户称呼（`叫我 XX`，学 SillyTavern {{user}} 注入 system prompt；人设 `# 称呼:` 兜底）
- 管理员认领码（6 位数字，面板展示，私聊+认领码双条件，防抢注）
- 上线通知默认关（`BOT_ONLINE_NOTIFY`，曾因频繁重启轰炸管理员导致 QQ 风控禁言——实测事故）

## 4. 已修复（上一轮评审，已验证，不必重查）

面板 CSRF（Content-Type/Origin/Sec-Fetch 三层 415/403）、人设名 XSS（textContent+白名单+CSP）、
Key 全接口掩码、.env 注入防护（禁 \r\n\0 + 键名白名单）、日志脱敏（sk-/DEEPSEEK_API_KEY/Bearer）、
端口精确列解析、会话锁+重置代数、同值保存不重启、发送被 QQ 风控拦截不崩溃（ActionFailed 捕获）。

## 5. 已知问题/待办（诚实清单）

1. QQ 图片 URL 疑点（见 3.3）——最想让你审的一条
2. 抖音选择器未经真实登录态验证（见 3.2）
3. 面板仍无鉴权（仅本机绑定+Host+CSRF 三层）；多用户场景需 token
4. 协议端是面板子进程，面板退出连带重启（用户接受现状；P1-2 方案待定）
5. 抖音每日额度/点赞率写死在代码常量，未做面板配置
6. mock 测试（tools/mock_onebot_test.py）会短暂改写 .env 的 SUPERUSERS（有 try/finally 还原）

## 6. 已踩坑（别花时间重发现）

- 中文 Windows netstat 输出 GBK，subprocess 必须 errors="replace"
- NapCat 实际读内层 `napcat/napcat/config/onebot11_<QQ>.json`，且要给所有历史账号都写
- 反向 WS 客户端必须带 X-Self-ID；事件缺 message_id 被静默降级；回信走 send_msg 动作
- 面板异步接口禁止同步阻塞（曾致整面板假死）；onebot v11 类名是 FriendAddNoticeEvent
- QQ 发送端风控：`Ban state forbit write req` = 临时禁言，新号高频私聊同一个人易触发
- DrissionPage 用法：page.ele("css:..."), page.scroll.down(px), page.get_screenshot(path=)

## 7. 验证工具

```bat
.venv\Scripts\python.exe -m app.check_api          :: 模型连通
.venv\Scripts\python.exe tools\mock_onebot_test.py :: E2E 回归（不碰真实 QQ，自动还原配置）
.venv\Scripts\python.exe -m ruff check app tools --select F,E9,ASYNC,B,UP041
```
