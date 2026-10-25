# gudum（deepseek-qq-bot）代码评审交接 v3

> 代码快照：2026-10-25 晚（v3：v2 修复落地 + 网页版抖音移除 + 安卓真机版上线后）
> 项目路径：`C:\Users\john\.zcode\workspace\default\deepseek-qq-bot`
> 技术栈：Python 3.13 · NoneBot2 + OneBot v11 · FastAPI 本地面板 · NapCat 协议端 · uiautomator2（可选真机）
> 用途：交给评审 AI 做检验。自包含。行号可能漂移，以实际代码为准。

## 0. 给评审 AI

请先通读本文与 `app/` 源码，重点审第 3 节（v2 修复落实验证 + v3 新增）。
只读评审：不改文件、不动 `napcat/`、不读写真实 `.env` 值、**绝不触碰已连接的安卓真机**
（不要调用 douyin.connect/start——测试者手机可能正插在电脑上）。
输出：问题清单按 P0-P3 分级 + 位置 + 修复建议（带代码）。

## 1. 概述与架构

开源免费的本地 QQ AI 聊天搭子：任意 OpenAI 兼容模型 API + QQ 小号扫码，私聊/群@/发图聊天，
多人设库、会话记忆、本地控制台、可选插件。

```
控制台 panel   127.0.0.1:8090  app/panel.py + panel_page.html
机器人内核     127.0.0.1:8081  NoneBot2(app/main.py) + 内置插件(app/plugins/*)
QQ协议端 NapCat               app/napcat_runner.py 拉起，反向 WS 连内核
安卓真机（可选）              uiautomator2 over adb，app/douyin.py
```

## 2. v2 评审修复（已实施+已验证，请抽查勿重查）

- P0-1 掩码 Key：前端切换提供商不回填 Key；服务端 `_looks_masked()` 拒绝；
  `/api/test` 按 base_url 只查用户自己的档案
- P1-1 保存真重启：before 快照前移 + `_restart_kernel_if_running()`（身份校验/等端口/轮询 healthz）
- P1-4 插件安装后按 `healthz.plugins` 验证真加载；MARKET 显式 module 字段
- P1-5 看图 base64：`app/vision.py`（域名白名单/≤6MB/魔数→data URL），失败不调模型
- P1-3/P1-6/P1-4 会话锁/端口精确解析/日志脱敏强化
- P2 全批：群聊指令 `_cmd_gate`、昵称白名单、认领码 secrets+TTL+限次、TTS 出锁+超时、
  `_split_chunk` 句切、无界字典 `_trim`、X-Frame-Options 等（详单见 git log 0fecf4a）
- 回归：`tests/test_fixes.py` 15 用例（离线 mock，绝不触碰真机/真实 .env）

## 3. v3 新增（本轮评审重点）

### 3.1 安卓真机刷抖音（`app/douyin.py` + `app/plugins/douyin.py`）——最大新增
- 历史：网页版（DrissionPage 抓画面）已**整体移除**（提交 2883ea8，用户判定"截屏≠刷视频"）；
  现为 uiautomator2 控制安卓真机：真滑动/真声音/真双击点赞
- 线程模型：`dev_lock` 保护 device 引用；AI 决策在同步线程里每次 `asyncio.run()` 新建客户端
  （AsyncOpenAI 连接池不跨循环复用——修过一个 coroutine 未 await 的 bug）
- 会话循环：连接→开抖音→解锁检查→两帧截图（间隔4~6s）→视觉模型决策→执行→
  连续刷6~12min歇1~3min；AI 连续 3 次失败=结束会话（无脚本兜底，用户要求删掉脚本模式）
- 限幅：单次≤60min、每日≤120min（data/douyin_daily.json 原子写）、仅管理员
- adb：缺失时自动从 Google 下载 platform-tools 到 tools/adb/（`download_adb`）
- 已知风险请评审：
  a) `_frame()` 对 u2 返回类型的 PIL/numpy 双路处理是否稳
  b) 手机中途拔线/锁屏 的路径（现仅记 last_error 结束会话，会不会留半截状态）
  c) `_double_tap_like` 双击间隔 0.08s 是否会被抖音判定非人类
  d) `connect()` 首次 u2 初始化需要手机上确认安装——失败路径的用户引导是否够

### 3.2 聊天上下文注入抖音状态（`app/plugins/chat.py` `_do_chat`）
- 用户消息含"抖音/视频/刷"时，注入系统消息：手机连接/是否在刷/最近5条 AI 决策（带理由）
- 已实测：手机未连时问"视频怎么样"→如实答"没在刷"（此前会瞎编"不存在"）
- 请评审：注入的是否该包"不可信"外壳；决策文本来自模型输出，是否有注入面

### 3.3 其他 v3
- 人设四段格式新增 `# 称呼:`（用户自设称呼优先，学 SillyTavern {{user}}）
- 上线通知默认关（`BOT_ONLINE_NOTIFY`，曾因频繁重启轰炸管理员→QQ 风控禁言，实测事故）
- 发送被 QQ 风控拦截（Ban state forbit write req）捕获 ActionFailed 记日志不崩
- 群聊指令门 `_cmd_gate`（群内需@或/前缀）

## 4. 已知问题/待办（诚实清单）

1. 真机自动化违反抖音用户协议，有账号风控风险（文档已声明，限幅写死）
2. 真机 AI 每条视频 2 次截图+1 次视觉调用：成本与节奏用户自担（面板无预算限制——P1-7 类老问题仍在）
3. 面板无鉴权（本机绑定+Host+CSRF 三层）；多用户场景需 token
4. 协议端是面板子进程（面板退出连带重启；用户接受现状）
5. 真机 v1 未做：评论（仅人工）、搜索、翻评论区（网页版有、真机版还没移植）
6. `tests/test_fixes.py` 曾因真机在位误触真实会话（已修：mock connect + tearDown stop）——
   评审请检查是否还有别的测试会碰真环境

## 5. 已踩坑（别重查）

- 中文 Windows netstat GBK → subprocess 必须 errors="replace"
- NapCat 读内层 napcat/napcat/config/，且要给所有历史账号写配置
- 反向 WS 必带 X-Self-ID；OneBot 事件缺 message_id 被静默降级；回信走 send_msg
- 面板异步接口禁止同步阻塞；onebot 事件类名 FriendAddNoticeEvent
- QQ 发送端风控 Ban state forbit write req = 临时禁言
- DrissionPage 已随网页版移除；真机用 uiautomator2 3.7.1（u2.connect 需 atx 代理就绪）
- AsyncOpenAI 客户端不能跨事件循环复用（同步线程里每次 asyncio.run 须新建客户端）

## 6. 验证工具

```bat
.venv\Scripts\python.exe -m app.check_api          :: 模型连通
.venv\Scripts\python.exe -m unittest discover tests :: 15 用例（不碰真机/真实 .env）
.venv\Scripts\python.exe -m ruff check app tools tests --select F,E9,ASYNC,B,UP041
```

## 7. 评审建议关注点（按优先级）

1. `app/douyin.py`：线程/设备生命周期、asyncio.run 每次新建客户端的正确性、异常路径是否留僵尸状态
2. `app/plugins/chat.py`：_do_chat 的注入面（搜索资料、抖音状态、称呼、vision）是否都可信边界清晰
3. `app/panel.py`：save/test/providers 的 Key 流转是否还有掩码泄漏路径
4. `app/plugin_manager.py`：local 插件安装/卸载与内核重启验证的竞态
5. Windows 特有坑有没有漏网
