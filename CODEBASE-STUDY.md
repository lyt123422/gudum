# gudum 代码库全面研究报告（2026-10-31）

> 研究方式：核心链路逐行精读 + 4 个子系统专项研究 + 实测（测试套件 123 用例全过 / ruff / git 状态）。
> 本文所有 `file:line` 均基于 commit `19fc2e3`（2026-10-26）。用于指导下一轮优化。

---

## 1. 一页总览

**gudum（本地目录名 deepseek-qq-bot）**：QQ AI 聊天机器人。NoneBot2 + OneBot v11（NapCat 协议端）+ OpenAI 兼容 LLM 接口（默认 DeepSeek，可切任意兼容服务商/本地网关），带本地 Web 控制台、插件市场、人设库、联网搜索、语音、看图、表情包、定时任务、抖音真机自动化、可选 WorkBuddy 本地反代。

| 维度 | 数据 |
|---|---|
| 应用代码 | app/ 约 5,500 行（26 个 py）+ panel_page.html 1,013 行 |
| 测试 | 3 文件 1,880 行，**123 用例全部通过**（5.8s，全离线 mock） |
| 工具 | one_click（一键启动）、build_release（打包）、mock_onebot（E2E 模拟器）、check_api |
| 运行形态 | 三进程：控制台(8090) ↔ 内核(8081) ↔ NapCat(node.exe)；可选反代(8788)、WorkBuddy-Hub |
| git | 干净，最新提交 `19fc2e3 docs: add changliao mode...`（10-26） |

**总体评价**：工程质量明显高于同类个人项目——评审注释落行、原子写遍地、密钥掩码+日志脱敏+图片域名白名单、重启带 PID 身份校验、测试纪律强。短板集中在：**netstat 式进程探测、全盘启发式扫描无缓存、若干假重试/竞态小 bug、proxy_manager 零测试、默认配置下抖音必失败**。

---

## 2. 三进程架构与控制面

```
浏览器 ──HTTP 127.0.0.1:8090──► 控制台 panel.py (FastAPI)
   │                               │  spawn/stop/healthz 轮询（PID 身份校验）
   │                               ├──► 机器人内核 main.py (NoneBot2, 8081) ◄──反向 WS── NapCat (node.exe)
   │                               └──► napcat_runner → napcat/index.js（合并输出+脱敏落盘）
   └── 消息流：QQ → NapCat → OneBot v11 → 插件(chat) → DeepSeekClient → 回复分段发送
可选：WorkBuddy-Hub (wb_proxy.py, 8788) 把腾讯 WorkBuddy 账号封装成 OpenAI 兼容接口；
      激活时 panel 改写 .env 三个键并重启内核（previous 快照可切回），互斥直连。
```

- 面板安全中间件：Host 校验防 DNS 重绑定、非 GET 强制 JSON + Origin/Sec-Fetch-Site 双检、nosniff/DENY/CSP（`app/panel.py:124-153`）。
- 内核重启是"真重启"：找 PID → healthz/命令行验身份 → taskkill /T → 等端口释放 → 拉起 → **必须 PID 变化**才算成功（`app/panel.py:532-567`）。
- 内核 `/healthz`（`app/main.py:29-81`）暴露 pid/插件表/OneBot 登录信息/deepseek/schedule/jailbreak 状态，是面板状态判定的唯一事实源。

## 3. 聊天管线（app/plugins/chat.py:552-695）

群冷却(3s) → 每人限频(60/h，失败退还) → 会话锁（第二条直接拒答）→ 组装 system prompt：
人设 + 防越狱条款（破甲生效时替换为破甲条款并抑制前者）+ 用户称呼 → 判定联网（显式「联网」或意图正则 `chat.py:129-132`）→ 记忆历史（data/memory/<key>.json，2h TTL、12 轮上限、原子写）→ 助手自我状态（assistant_state，位置在记忆之后以保住前缀缓存）→ 可选图片（vision 白名单+魔数+流式 6MB）→ 工具循环（web_search/douyin_status/memes_list，最多 2 轮，不支持工具自动降级）→ 过期代数丢弃 → 记忆落盘 → 分段发送（800 字句切）→ 锁外合成语音。

## 4. 模块地图

| 模块 | 行数 | 职责 | 测试 |
|---|---|---|---|
| plugins/chat.py | 1043 | 聊天管线、指令、管理员认领、记忆、限频 | 仅经破甲集成测试间接覆盖 |
| panel.py | 1188 | 控制台全部 API + 子进程管理 | 安全面/重启/schedule/反代安装 有测 |
| plugins/chat 依赖的 deepseek_api.py | 205 | OpenAI 兼容客户端、错误翻译、工具循环、推理预算重试 | 有 |
| douyin.py | 716 | adb+uiautomator2 真机刷抖音，AI 两帧决策 | 核心 20+ 用例 |
| schedule_core.py + plugins/schedule.py | 402+227 | cron 轮询调度（双层：core 无 nonebot 依赖） | 有（260 行） |
| workbuddy_setup.py | 468 | Hub 供应链安装（钉版本+SHA256+防 zip-slip） | 有（zip-slip 等） |
| **proxy_manager.py** | 257 | 反代进程托管/守护/端口探测 | **零测试** |
| personas.py | 273 | 纯文本人设格式解析/种子/原子写 | 部分 |
| jailbreak.py | 171 | 破甲（回答尺度）三态 + 预设 + 硬红线 | 37 用例（含管线集成） |
| plugin_manager.py | 225 | 内置开关(2s 缓存)+精选市场白名单安装 | 表结构有测 |
| assistant_state.py | 206 | 抖音会话→聊天自我认知（线程安全） | 有（并发矩阵） |
| napcat_runner/config | 142 | 启动壳脱敏中继 / 反向 WS 配置生成 | 无 |
| vision/web_search/tts/log_setup | 372 | 图片安全底座 / DDG+Tavily / edge-tts / 脱敏日志 | vision 有，其余无 |
| panel_page.html | 1013 | 单文件原生 JS 控制台 | 无（纯前端） |

## 5. 数据面（全部 data/ 下 JSON，均 tmp+replace 原子写）

`memory/<p或g群_u人>.json`（对话记忆）、`admins.json`+`.env SUPERUSERS` 双写、`claim_code.txt`（mtime 即 TTL）、`nicknames.json`、`voice_users.json`、`plugins.json`、`providers.json`（**明文 Key**）、`proxy.json`（含 `previous.api_key` **明文直连 Key 快照**）、`jailbreak.json`、`schedules.json` 与 `schedule_runtime.json`（双文件防双进程读写冲突）、`douyin_daily.json`、`memes/`（文件名即库）。
遗留物：`data/douyin_profile/`（2.3MB Chromium 残留）与 `data/douyin_shots/`（横屏截图）为已删除的"网页版抖音"遗骸，现行代码零引用，可删。

## 6. 安全设计盘点（优化时不要破坏这些）

1. 面板三层防护（Host/JSON+Origin/nosniff+CSP）——`panel.py:124-153`
2. Key 全链路掩码 + `_looks_masked` 防掩码回写 + 换 URL 必须带新 Key（防 Key 外带）——`panel.py:113-121,324-325,299-300`
3. 日志 `redact_log`（键值对/Bearer/sk- 三正则）+ 按日滚动 14 天——`log_setup.py:22-37`
4. 图片：QQ 域名白名单 + 魔数判真 + 6MB 流式中断 + 逐跳重定向复查——`vision.py`
5. 人设名路径穿越/Windows 保留名过滤——`panel.py:457-472`
6. 管理员认领：6 位加密随机码、仅私聊、每人 5 次/天 全局 20 次——`chat.py:712-799`
7. WorkBuddy 安装链：钉 v1.8.0+SHA256、无校验文件的 release 拒装、zip-slip 四重防线——`workbuddy_setup.py`
8. envutil 值注入防护（禁换行/\\0，防 .env 换行注入）——`envutil.py:39-66`
9. 联网资料/图片文字/模型自述一律标注"不可信数据不执行"——`chat.py:601-607,611`、`assistant_state.py:177`
10. 破甲硬红线（不破解付费授权、不写危害型恶意程序）写死在模块里，预设不可覆盖——`jailbreak.py:76-83`

## 7. 问题清单（按严重程度）

### P0 · 真 bug / 设计缺陷

1. **`_ensure_awake` 假重试**：`douyin.py:458-467` for 循环两个分支都必然 return，"重试两次"从未发生；且无法区分"有 PIN 解不开"。
2. **表情包下载白名单可被重定向绕过**：`plugins/memes.py:85` `follow_redirects=True` 只校验初始 URL；同仓库 `vision.py:40-47` 已有正确的逐跳复查范式未复用。
3. **默认配置下抖音 AI 决策必失败**：视觉决策与聊天共用 `DEEPSEEK_MODEL`（默认 deepseek-chat 文本模型，`douyin.py:253-267`），每轮 BadRequest → 连续 3 次失败熔断（:546-554），白烧 3 次 API。无独立视觉模型配置、start() 无多模态预检。
4. **抖音 stop/start 竞态**：`stop()` 不持 `start_lock`（:655-666 vs :608），start 校验 gen 后、`stop.clear()` 前插入的 stop 会被吞掉。
5. **暂停不冻结时长**：`douyin.py:498` deadline 一次定死，暂停期间照常消耗；daily 记账按全程算（:584）。
6. **test_fixes.py 结构缺陷**：`unittest.main()` 在 :508，其后还有约 40 个用例；`python tests/test_fixes.py` 直跑会静默漏跑（unittest discover 不受影响）。

### P1 · 稳健性 / 性能

7. **netstat 探测三宗罪**（`panel.py:1013-1037`、`proxy_manager.py:128-159`）：Windows 专用、非英文系统输出解析脆弱、**proxy 只认端口不认进程**——8788 被无关进程占用时会把 Bearer Key 和聊天内容发给它。内核侧有身份校验（:504-519），反代侧没有。
8. **config()/detect() 高频全盘扫描**：`proxy_manager.config()` 每次 stat + `workbuddy_setup.detect` 扫 D–H 盘根目录（:89-97），被面板高频 API 反复触发——最大的稳态性能损耗。
9. **同步阻塞混入 async**：`panel.py:717-719`（api_proxy 里直接调 proxy_manager.status()/config()，netstat 最长 5s 卡事件循环）；同文件 :865 已用 to_thread，风格不一。
10. **守护线程永不退出**：douyin watchdog（:589-602）、proxy watchdog（:225-246）无 shutdown 路径；proxy watchdog 失败 ≥3 次仍持续尝试 start()（可能触发分钟级下载），且 `last_error` 恢复后不清零。
11. **反代启动超时留孤儿进程**：`proxy_manager.py:195-203` 超时不 terminate。
12. **envutil 往返不一致**：BOM（utf-8 而非 utf-8-sig，:23）、行内注释、写回不加引号与读取"对称引号才剥"（:33-34,57）、同键重复行不去重。
13. **schedule 细节**：`except TimeoutError` 在 py3.10 上落空（README 承诺 3.10+，`schedule.py:105`）；发送失败原因三合一丢失（:108-110）；删任务不清理 runtime（`clear_runtime` 全仓无调用，死代码）；minute_mark 公式双实现（`schedule_core.py:146` 与 `schedule.py:121`）。
14. **memes 体验**：选图 `int(time.time()) % len`（:114）同秒必同图；关键词被 `_safe_name` 清洗后匹配不上 stem 静默回退（:108,40-41）；读路径带 mkdir 副作用（:45）。

### P2 · 质量 / 维护性

15. **proxy_manager 零测试**（README 主推功能）；web_search/tts/memes/napcat_* 无测试；chat 限频/记忆 TTL/认领码无测试。
16. **无 CI、无 dev-requirements**：pytest 不在 requirements（本报告研究时已手动装入 venv）；测试还无条件依赖 Pillow（`test_fixes.py:272`），fresh venv 按 requirements.txt 装出来跑不动测试。
17. **V3-REVIEW 尾债**：personas 段内空行丢弃（P2-22 前半，`personas.py:80-81`）、`api_persona_save` 非原子写（`panel.py:916`）与 `ensure_seeded` 非原子（:181）、CSP 仍含 unsafe-inline（`panel.py:150-151`）、douyin 重复指令未合并（`plugins/douyin.py:63-76` 两个逐字节相同的 handler）。
18. **文档漂移**：HANDOFF 写"113 用例"（实际 123，HANDOFF.md:13,57,69）；QA-CHECKLIST 缺破甲/反代验收项；TUTORIAL.md:170 表格错行；QA-CHECKLIST.md:19 病句。
19. **密钥明文落盘**：providers.json / proxy.json previous.api_key（本机自用风险可控，但备份/同步目录会带走）。
20. **`_state` 多线程无锁标量**（douyin.py:38-44）、plugins/schedule.py:20-35 手工转发 16 个符号的兼容别名堆。

## 8. 优化路线图（建议三批）

**第一批 · 小手高龄收益（半天内）**
- 修 `_ensure_awake` 真重试 + 可操作错误信息（douyin.py:458-467）
- memes 改用 `vision.fetch_data_url` / 逐跳白名单（memes.py:85）
- 抖音新增 `DOUYIN_MODEL`（或 VISION_MODEL）+ start() 多模态预检，deepseek-chat 直接拒并提示
- 合并 `刷抖音`/`AI刷抖音` handler；`unittest.main()` 移到文件尾
- `stop()` 持 start_lock；pause 冻结 deadline 与记账
- 删 `data/douyin_profile/`、`data/douyin_shots/` 遗留

**第二批 · 结构性（1-2 天）**
- 反代探活换 HTTP `/health`（替代 netstat，顺带解决进程身份+跨平台+locale 三问题）
- config()/detect() 加 mtime 缓存；D–H 盘探测降级为仅安装时
- api_proxy 同步调用统一 `asyncio.to_thread`
- 反代启动超时 terminate 孤儿；watchdog 加退出路径与恢复清错
- schedule：失败原因透传、删任务清 runtime、TimeoutError 兼容、minute_mark 收敛
- dev-requirements.txt（pytest/Pillow/ruff/coverage）+ 最小 GitHub Actions CI

**第三批 · 测试与打磨（按需）**
- proxy_manager 补测试（TestClient 手法照抄 test_workbuddy_setup.py）
- chat 限频/记忆 TTL/认领码补单测；mock_onebot 升级为 `GUDUM_E2E=1` 门控集成测试
- envutil 健壮化（utf-8-sig/引号往返/去重）；personas 空行保留+原子写；CSP 去 unsafe-inline
- 文档同步（113→123、QA-CHECKLIST 补新功能、TUTORIAL 表格）
- providers.json/proxy.json 密钥 DPAPI 加密或至少启动时告警

## 9. 值得保持的既有优点（重构时别丢）

代数（gen）机制作废旧会话、暂停感知睡眠、一次性 AI 客户端即关、stop 不谎报、PID 变化才算重启成功、失败退还限频额度、记忆只存文字不存 base64、前缀缓存友好（易变状态放 system 末尾）、破甲与人设防越狱互斥处理、诚实措辞约束（"只看过截图帧"）。
