# FACTS.md · 事实核对表（v4，2026-10-25）

> 每条断言对应代码位置；`TODO` = 无法从代码确认，文档中不使用该断言。

| # | 断言 | 依据（文件:行） |
|---|---|---|
| F1 | 看图默认关闭，需 `.env` 设 `VISION_ENABLED=true` 且重启内核 | app/plugins/chat.py:72（默认 `"false"`）；.env.example:55 |
| F2 | 看图需多模态模型，文本模型报"不支持看图" | app/deepseek_api.py:50-59（BadRequestError→图片提示）；chat.py:538-539（图片安全声明） |
| F3 | 抖音 AI 决策用 `DEEPSEEK_MODEL`，默认 deepseek-chat（文本模型） | app/douyin.py:258 |
| F4 | AI 连续 3 次决策失败 → 结束会话 | app/douyin.py:550-553 |
| F5 | 抖音单次 ≤60 分钟、每日 ≤120 分钟 | app/douyin.py:35-36 |
| F6 | 抖音每条视频取 2 帧截图 + 1 次视觉调用，AI 只看截图帧（无声音） | app/douyin.py:_ai_session_loop 两帧逻辑；render_self_state 诚实措辞行（assistant_state.py） |
| F7 | 抖音 AI 连续失败=会话结束，无脚本兜底（脚本模式已移除） | app/douyin.py:553；git 2883ea8 |
| F8 | 认领码 6 位、10 分钟有效、每人每天 5 次、全局每天 20 次 | app/plugins/chat.py:632/660/662/680 |
| F9 | 认领码显示在控制台「👤 QQ 账号与权限」卡片 | app/panel.py:/api/status claim_code；app/panel_page.html:#claim-code |
| F10 | 已有管理员时用 `设置管理员 <QQ>` 加人；面板管理员字段也可填 | app/plugins/chat.py:_persist_admins；panel /api/save SUPERUSERS |
| F11 | 群聊指令需 @机器人 或 / 前缀；私聊无前缀 | app/plugins/chat.py:386-389 `_cmd_gate` |
| F12 | 记忆：私聊每人、群聊每人每群；默认 2 小时不聊清空、重启不丢；`重置` 清当前会话 | app/plugins/chat.py:56（默认2）+ memory 落盘 data/memory/ |
| F13 | 内置插件默认开关：联网✓ 语音✓ 表情包✓ 抖音真机✗ | app/plugin_manager.py:22-32 |
| F14 | 市场安装抖音真机 = 装 `uiautomator2==3.7.1` + 开启，不需重启内核 | app/plugin_manager.py:38 + local 安装分支 |
| F15 | 第三方插件安装后需内核重启加载；内核未运行时提示"下次启动生效" | app/panel.py:_restart_kernel_if_running + toggle 分支（"下次启动生效" 行467） |
| F16 | 面板只监听 127.0.0.1:8090；无账号密码，仅 Host/CSRF 层防护 | app/panel.py:31 + `_guard` 中间件（"仅限本机访问" 行127） |
| F17 | 内核端口跟随 .env `PORT`，默认 8081；面板 healthz 可看 | app/panel.py:32 |
| F18 | 二维码约 150 秒过期；面板只端新鲜码，过期提示刷新 | app/panel.py:757 |
| F19 | "登录一次以后免扫码"不成立——强制停止/环境变化会使快速登录失效 | 实测日志（18:58 快速登录失效）；代码无永久保证 |
| F20 | 聊天自我状态注入模式 `ASSISTANT_AWARENESS=off/smart/full`，默认 smart | app/plugins/chat.py:550；.env.example:39 |
| F21 | 非管理员任何模式都看不到手机/视频/决策细节 | app/assistant_state.py:render_self_state 非管理员分支 |
| F22 | 抖音命令全部管理员专用；`连接手机/抖音状态/抖音帮助` 也管理员 | app/plugins/douyin.py:_guard（need_plugin/管理员校验） |
| F23 | 群管理员只见粗粒度（"正在忙/刚结束/出错"），无私聊细节 | app/assistant_state.py:render 非私聊分支 |
| F24 | AI 决策超时 25 秒、零重试；`stop` 8 秒 join，超时返回"停止中" | app/douyin.py:_ai_client(timeout=30,max_retries=0)、_ai_decide wait_for(25)、stop() |
| F25 | 会话中禁止重连手机（"先发停止抖音"） | app/douyin.py:connect 首行 |
| F26 | 手机拔线/锁屏 → 会话结束并记录原因，状态不再假报已连接 | app/douyin.py:_device_alive/_ensure_awake/循环 break 路径 |
| F27 | 手机在桌面/其他 App 时会尝试自动打开抖音；失败则提示手动 | app/douyin.py:_ensure_awake 拆分后主循环（v4 P1-1） |
| F28 | 首次启动控制台自动建 venv 并装 requirements（国内源提示） | 控制台.bat:6-13 |
| F29 | 保存模型配置成功会自动重启内核（PID 变化才算成功）；QQ 登录不受影响 | app/panel.py:_restart_kernel_if_running（PID 比对） |
| F30 | 保存/插件切换在内核重启失败时如实报错，不谎报生效 | app/panel.py（v4 P1-6 修复） |
| F31 | 联网默认 DuckDuckGo 免费无 Key；Tavily 可选 | .env.example SEARCH_PROVIDER/注释 |
| F32 | 语音 = edge-tts 免费；`语音开` 按用户记忆开关；人设 `# 语音:` 指定音色 | app/tts.py；chat.py _voice_users |
| F33 | 聊天自我状态三模式对"没在刷"的处理：smart 仅关键词命中时说"没在刷" | app/assistant_state.py:render 尾部分支 |
| F34 | `状态`/`人设`/`切换人设`/`设置管理员`/抖音全部命令 = 管理员；其余聊天/表情/语音/联网/人设列表/叫我/帮助/重置 = 所有人 | app/plugins/chat.py 指令注册与 handler 校验；douyin _guard |
| F35 | 插件 nonebot-plugin-chatgpt 与内置聊天抢消息，二选一 | app/plugin_manager.py MARKET 描述 |
| F36 | 401=Key 错 / 402=余额不足 / 404=模型名错 / 超时=服务无响应 | app/panel.py:_llm_test 错误映射 |
| F37 | 发送被 QQ 风控拦截（Ban state forbit write req）→ 记日志等待，不崩溃 | app/plugins/chat.py ActionFailed 分支 |
| F38 | 视觉调用超时 25 秒、零重试；成本=每条视频 2 截图+1 调用 | app/douyin.py:_ai_client/_ai_decide |
| F39 | 面板启动自动开浏览器到 127.0.0.1:8090；关窗口=退出 | app/panel.py:main + 控制台.bat |
| F40 | git clone 仓库不含 napcat/（被 .gitignore），需发布包或自行获取 | .gitignore + HANDOFF |

## TODO（无法从代码确认，文档中未使用）

- NapCat WebUI 首次密码获取细节（依赖 NapCat 版本，文档指向 NapCat 官方说明）
- 手机锁屏密码类型对 `unlock()` 的实际效果（需真机，NOT RUN）
- 抖音"双击点赞"在全部版本的兼容性（NOT RUN）
