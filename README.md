# gudum · QQ AI 聊天搭子

填上模型 API（DeepSeek / 智谱 / 通义 / Kimi / Ollama 等任意 OpenAI 兼容端点）和一个 QQ 小号，
就拥有一个自己的 QQ AI 聊天搭子：**私聊直接聊，群里 @它 聊**，带多套人设、会话记忆（重启不失忆）、
本地控制台、定时任务，以及可选的 WorkBuddy 反代和安卓真机抖音。开源免费，数据都在你自己电脑上。

```
你的 QQ 小号 ── NapCat(协议端, 发布包内置) ──反向WS── NoneBot2 + DeepSeek 插件 ── DeepSeek API
```

> **源码仓库**不包含 `napcat/` 和 `vendor/WorkBuddy-Hub/`（体积大/含第三方运行时）；
> **Windows 发布包**已包含干净的 NapCat + WorkBuddy-Hub，解压后双击 `一键启动.bat` 即可。
> 如果你是 `git clone` 源码，需要按 TUTORIAL 单独获取 NapCat；WorkBuddy 会在首次登录时自动下载。

## 准备

| 需要 | 说明 |
|---|---|
| Windows 电脑 + Python 3.10+ | 安装时勾选 **Add python.exe to PATH**；发布包已内置 NapCat、Node 和 WorkBuddy-Hub |
| DeepSeek API Key | 注册 <https://platform.deepseek.com> → API Keys → 创建（sk- 开头） |
| QQ 小号 | 给机器人用，**不要用主号**（协议端有风控/封号风险）；小号先加你主号好友 |

## 一键启动（发布包推荐）

双击 **`一键启动.bat`**：

1. 首次自动创建 `.venv`、安装依赖、从 `.env.example` 生成 `.env`。
2. 启动本地控制台并打开浏览器。
3. 如果你已经在 `.env`/面板里填了模型 Key 和 QQ 小号，它会继续自动启动内核和 QQ 协议端；
   没填则只打开控制台，等你填完再启动。
4. 第一次登录 QQ 需要手机扫码；扫码后账号会自动回写。

## 最简启动：控制台

双击 **`控制台.bat`**，浏览器自动打开 `http://127.0.0.1:8090`，一个页面搞定全部：

1. **🧠 模型配置**填 Base URL / API Key / 模型 → 点「🔌 测试连通」看到 ✅ → 点「💾 保存并重启机器人」
2. **👤 QQ 账号与权限**填小号 QQ / 管理员 QQ（用 WorkBuddy 反代时，先看下面「反代」一节）
3. 运行区点「▶ 启动机器人内核」
4. 点「▶ 启动QQ协议端」→ 页面出现二维码 → 手机 QQ（小号）扫码
5. 顶部三个状态灯全绿（内核运行中 / QQ 已连接 / Key 已填）即成功，主号加小号好友开聊

> 控制台只监听本机（127.0.0.1）。用 `start.bat` 手动启动的内核，控制台同样能看到状态、也能停止。

## 手动启动（三步，效果等同）

1. **填配置**：用记事本编辑项目根目录的 `.env`，至少改三处：
   - `DEEPSEEK_API_KEY=` 填你的 Key
   - `SUPERUSERS=["你的主号QQ"]`（管理员，记得去掉 10000 占位）
   - `NAPCAT_ACCOUNT=` 机器人小号 QQ
2. **启动机器人内核**：双击 `start.bat`（首次自动装依赖，等它出现启动日志）
3. **启动 QQ 协议端**：双击 `start_napcat.bat`，手机 QQ 用小号**扫码登录**

之后：拿主号加小号好友，私聊直接发消息就能聊；拉群后 @小号 也可以。

> 两个窗口都要保持开着。关掉 = 机器人下线。不要和其他机器人（如 quark-media-bot）
> 同时登录**同一个** QQ 号，会互踢。

## 指令

| 指令 | 谁能用 | 说明 |
|---|---|---|
| 直接发消息 | 所有人 | 私聊即聊 |
| @机器人 + 消息 | 所有人 | 群聊触发 |
| `重置` | 所有人 | 清空当前会话记忆 |
| `联网 <问题>` | 所有人 | 联网搜索后回答；问新闻/天气等时效问题会自动联网 |
| `语音开` / `语音关` | 所有人 | 回复附带角色语音（edge-tts 免费配音，人设可指定音色） |
| `表情包 [词]` | 所有人 | 随机/按词发一张表情（`斗图`、`来个表情` 同义） |
| `存表情 [名]` | 所有人 | **回复**一张带图的消息发送，把它收进表情库 |
| `表情列表` | 所有人 | 查看表情库 |
| `人设列表` | 所有人 | 查看人设库 |
| `切换人设 <名>` | 管理员 | 换一套人格，如 `切换人设 傲娇猫娘小雪` |
| `人设 <描述>` | 管理员 | 改当前人设的性格（保留开场白/示例对话） |
| `状态` | 管理员 | 查看模型/Key/当前人设/记忆会话数/限频配置 |
| `定时任务 ...` | 管理员 | 定时向群/好友发消息；支持 添加/列表/删除/开关/立即 |
| `帮助` | 所有人 | 指令菜单 |

记忆规则：私聊每人一份、群聊每人每群一份，互不串；`MEMORY_HOURS`（默认 2 小时）
不说话自动清空，重启不清空（存在 `data/memory/`）。

## 管理员

首次使用：**用你的主号私聊机器人发 `我是主人 <认领码>`**。认领码在控制台
「👤 QQ 账号与权限」卡片里，10 分钟有效；每人每天 5 次、全局 20 次尝试上限。
不认领的话，切换人设、`状态`、定时任务、抖音真机等管理员命令都用不了。
之后管理员可发 `设置管理员 <QQ号>` 添加更多管理员；控制台
「模型配置 → 👤 QQ 账号与权限」里也能直接填管理员 QQ。

| 角色 | 权限 |
|---|---|
| 👑 管理员 | 切换人设、修改人设、查看状态（含 Key 掩码）、设置其他管理员 |
| 👥 所有人 | 私聊/群@聊天、`重置` 自己的会话记忆、`人设列表`、`帮助` |

## 人设系统（多人格 / 开场白 / 示例对话 / 防越狱）

人设文件放在 `data/personas/<名称>.txt`，记事本直接改，格式：

```
# 名称: 傲娇猫娘小雪        ← 人设名（写在最上面）
# 防越狱: 关                ← 可选：关掉通用防越狱条款

[人设]
你是猫娘小雪，傲娇、嘴硬心软……   ← 角色描述（System Prompt 主体）

[示例]                        ← 可选：示例对话，模型会照这个语气说话
用户: 你好
你: 哼，才不是特地在等你喵

[开场白]                      ← 可选：别人加机器人为好友后，主动私聊的第一句话
……你加我干嘛喵？
```

内置三套：**默认伙伴 / 傲娇猫娘小雪 / 毒舌损友**。QQ 指令：`人设列表`、
`切换人设 <名>`、`人设 <描述>`；控制台「🎭 人设管理」页也能切换，立即生效。
每套人设生效时自动附加**通用防越狱条款**（防"你是 AI 吧""假装成别人"这类调戏），
想给某套人设关掉就写 `# 防越狱: 关`。

### 设计出处

本系统借鉴了以下开源项目的设计，感谢原作者：

- **AstrBot** —— 人格设定 = System Prompt 注入 + 多人格切换、人格锁定（防越狱）实践：
  <https://docs.astrbot.app> · <https://github.com/AstrBotDevs/AstrBot/discussions/8614>
- **SillyTavern Character Card V2** —— 开场白（first_mes）与示例对话（mes_example）独立成字段的思想：
  <https://github.com/malfoyslastname/character_card_v2>
- **MaiBot** —— "像群友一样聊天"的拟人理念（开场白延迟几秒发送、句末少句号等细节）：
  <https://github.com/Mai-with-u/MaiBot>

人设还可加 `# 语音: zh-CN-YunxiNeural` 一行，给这套人设指定专属音色（配合「语音开」使用；
音色列表见 <https://edge-tts.netlify.app>，全部免费）。

## 联网搜索与角色语音（配置在 .env）

- 联网：默认 DuckDuckGo 免费无 Key；`SEARCH_PROVIDER=tavily` + `TAVILY_API_KEY` 可换质量更好的搜索
- 语音：`edge-tts`（微软朗读接口）免费无 Key；`TTS_VOICE` 改默认音色
- 看图：`VISION_ENABLED` 默认**关**。要用图片：`.env` 设 `VISION_ENABLED=true`，并把模型换成多模态模型（qwen-vl / glm-4v / gpt-4o / gemini 等），重启内核
- 两者都有开关：`WEB_SEARCH_ENABLED` / `TTS_ENABLED`

## 反代（WorkBuddy，可选）

WorkBuddy-Hub 把 WorkBuddy 账号封装成本地 OpenAI 兼容接口。gudum 把它做成**每个用户本机自用**：
你登录的是自己的 WorkBuddy 账号，Key、额度和聊天内容都只经过你自己的电脑，不经过作者或他人的端点。

1. 面板「🔀 反代」→「🌐 登录 WorkBuddy」。没装 WorkBuddy-Hub 时，面板会自动从官方 GitHub Release
   下载 `workbuddy2api-hub-v1.8.0.zip` 到 `data/workbuddy-hub/`，校验 SHA256 后解压并启动（约 10 MB）。
2. 打开的 WorkBuddy 看板里点「+ 添加账号 (OAuth)」→ 浏览器登录自己的 WorkBuddy（国际版/国内版）；
   账号自动入库。检测到账号就绪后，gudum 会自动接入；也可回来点「🔎 检测并接入」。
   看板密码默认是 WorkBuddy-Hub 自带的 `admin`，首次登录后建议在设置里改掉。
3. 健康检查通过后会拉取 `/v1/models`、写模型源并重启内核；在「模型」下拉里选精确 ID，
   或手动输入后点「应用模型」/回车。
4. 点「⏹ 停止并切回直连」恢复启动前的直连模型。

> 每个用户都需要自己的 WorkBuddy 账号；不要使用别人的账号或 Key。反代默认只监听
> `127.0.0.1:8788`，同一台电脑之外访问不到。
>
> 模型名必须用 `/v1/models` 的精确 ID，例如 `deepseek-v4.1-flash`；`deepseek4.1flash` 这种写法会 404。
> 反代激活时，「🧠 模型配置」里的直连保存只存档案、不激活；要换反代模型必须在「🔀 反代」页操作。

**已有安装 / 离线安装（高级）**

- 已有 WorkBuddy-Hub：在反代页「高级」里填含 `wb_proxy.py` 的目录，或设置环境变量
  `WORKBUDDY_HUB_DIR`。gudum 会直接复用，不再下载。
- GitHub 下载慢：手动下载官方 Release 的 `workbuddy2api-hub-v1.8.0.zip`，放到
  `data/workbuddy-hub.zip`（或设置 `WORKBUDDY_HUB_ZIP` 指向它），再点登录即可离线安装。
- 指定其他版本：`WORKBUDDY_HUB_VERSION=v1.8.0` 或 `latest`；自定义镜像：
  `WORKBUDDY_HUB_URL` + `WORKBUDDY_HUB_SHA256`（必须提供校验值）。
- 安装目录默认是 `data/workbuddy-hub/`（已被 `.gitignore` 忽略）；卸载直接停掉反代后删除该目录。
- WorkBuddy-Hub 是第三方 MIT 项目：<https://github.com/ardeyouxipianyi/workbuddy2api-hub>；
  许可证与归属见 `THIRD-PARTY.md`。

## 定时任务（管理员）

面板「⏰ 定时任务」：填名称 / cron（分 时 日 月 周）/ 内容 / 目标（群号或 `friend:QQ号`）→ 保存。
同名保存=更新；列表里可开关/编辑/删除。先用 QQ 发 `定时任务 立即 <名字>` 测试发送，再等 cron。

- 例：`0 9 * * 1-5` = 工作日 9:00；`*/30 * * * *` = 每 30 分钟；`30 8 * * *` = 每天 8:30（周字段 0=周日）
- QQ 指令：`定时任务 添加 名字 | cron | 内容 | 群号`、`定时任务 列表/删除/开启/关闭/立即 名字`
- 依赖内核运行；停机期间错过的任务不会补发；发送失败会记录，不保证同一分钟重试
- 数据：`data/schedules.json`（任务定义）与 `data/schedule_runtime.json`（运行结果）

## 自检与排障

```bat
:: DeepSeek 连通性（填完 Key 先跑这个）
.venv\Scripts\python.exe -m app.check_api

:: 机器人内核健康检查（内核启动后浏览器打开）
http://127.0.0.1:8081/healthz
```

- **机器人不回话**：先看 `start.bat` 窗口有没有 `OneBot 已连接` 日志；没有就是协议端没连上，
  检查 `start_napcat.bat` 窗口的二维码/报错。
- **回「API Key 无效」**：跑上面的连通自检；402 = DeepSeek 余额不足，去平台充值。
- **日志在哪**：`data/logs/bot-日期.log`（已自动脱敏 Token）。
- **换小号**：改 `.env` 的 `NAPCAT_ACCOUNT`，重开 `start_napcat.bat` 扫码。

## 风险须知

- NapCat 为第三方协议端（逆向 NTQQ），存在小号被风控/封号的可能：**只用小号**、
  别拉超大群、别刷屏（默认已带限频和群冷却）。
- DeepSeek API 按量计费（约 1 元/百万 token 量级），Key 在你自己手里，费用自理；
  不想花钱可把 `DEEPSEEK_BASE_URL` 换成任何 OpenAI 兼容的免费服务。

## 项目结构

```
├── start.bat / start_napcat.bat   # 双击启动（内核 / 协议端）
├── .env                           # 全部配置（改这里）
├── app/
│   ├── main.py                    # NoneBot2 入口 + healthz
│   ├── deepseek_api.py            # 模型客户端（含工具调用/错误翻译）
│   ├── assistant_state.py         # 抖音/聊天共享状态
│   ├── schedule_core.py           # 定时任务核心（cron/任务定义/运行结果）
│   ├── proxy_manager.py           # WorkBuddy 反代进程管理
│   ├── workbuddy_setup.py         # WorkBuddy-Hub 下载/校验/定位/离线安装
│   ├── napcat_runner.py           # NapCat 启动壳（输出脱敏）
│   ├── napcat_config.py           # 自动生成 NapCat 反向 WS 配置
│   ├── check_api.py               # API 连通自检
│   ├── log_setup.py               # 日志按日落盘 + 凭证脱敏
│   └── plugins/
│       ├── chat.py                # 聊天：私聊/群@/记忆/人设/限频
│       ├── schedule.py            # 定时任务命令与调度 worker
│       └── douyin.py              # 抖音真机命令
├── napcat/                        # 协议端（328MB，内置 Node，无需另装）
├── THIRD-PARTY.md                 # 第三方组件许可（WorkBuddy-Hub 等）
└── data/                          # 记忆、人设、日志、反代安装（全在本机）
```

## 维护者：发布包构建

源码仓库不提交 `napcat/`、`vendor/`、`data/`、`.env`。要生成 Windows 发布包：

```bat
.venv\Scripts\python.exe tools\build_release.py --version v1.0.0
```

输出：

- `dist/gudum-v1.0.0/`（解压即用的目录）
- `dist/gudum-v1.0.0-windows-x64.zip`
- `dist/gudum-v1.0.0-windows-x64.zip.sha256`

发布包会包含干净的 NapCat、官方 WorkBuddy-Hub、`一键启动.bat`，并做校验确保没有
账号配置、二维码、日志、数据库或 `.env` 泄露。
