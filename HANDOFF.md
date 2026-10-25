# gudum（deepseek-qq-bot）交接文档 v4 · 外来 AI 开发任务书模板

> 快照：2026-10-25 晚（v4：shared assistant_state + 真机重写 + v3 评审修复全部落地）
> 项目：`C:\Users\john\.zcode\workspace\default\deepseek-qq-bot`
> 技术栈：Python 3.13 · NoneBot2 + OneBot v11 · FastAPI 本地面板 · NapCat 协议端 · uiautomator2 3.7.1（可选真机）

## 0. 给开发 AI 的硬约束（违反即返工）

1. **基线**：先 `git log --oneline | head -3` 确认在最新提交，所有改动基于它；改完单独 commit。
2. **禁触清单**：`napcat/`（第三方协议端）、真实 `.env` 的值（可读结构不许写/不许输出值）、`data/`（用户记忆/人设/登录态）、`tools/adb/`。
3. **绝不调用** `douyin.connect()` / `douyin.start()`——测试机手机可能正插着，会真刷。测试一律 FakeDevice/mock。
4. **安全措施不得削弱**：CSRF 中间件、XSS textContent、Key 掩码（`_looks_masked`）、日志脱敏、`.env` 注入校验、`_cmd_gate`——所有回归测试必须继续通过。
5. **验证闭环**：`.venv\Scripts\python.exe -m unittest discover tests`（当前 89 用例）+ `.venv\Scripts\python.exe -m ruff check app tools tests --select F,E9,ASYNC,B,UP041` 全绿才算完成。
6. **部署边界**：开发 AI 不重启内核/面板/QQ 服务（QQ 登录态在；重启由维护方执行）。需要运行时验证的写 NOT RUN + 手工步骤。
7. **最小 patch**：不改现有指令名/人设格式/记忆格式/面板结构；新功能优先做成**插件或独立模块**。

## 1. 架构（三进程 + 可选手机）

```
控制台 panel   127.0.0.1:8090  app/panel.py + panel_page.html（页面热加载，改 HTML 不用重启）
机器人内核     127.0.0.1:8081  NoneBot2(app/main.py) + 内置插件(app/plugins/*)
QQ协议端 NapCat               app/napcat_runner.py 拉起，反向 WS 连内核
安卓真机（可选）              uiautomator2 over adb，app/douyin.py
```

- 内核读配置在启动时：改 `.env`/配置类代码需重启内核才生效（QQ 登录不受内核重启影响）
- `panel_page.html` 每次请求现读：改前端 F5 即生效
- 共享状态：`app/assistant_state.py`（线程安全）——抖音会话线程写，聊天每轮读（第一人称自我状态注入）

## 2. 代码地图（改哪里看这里）

| 想做的事 | 位置 |
|---|---|
| 新 QQ 指令/聊天行为 | `app/plugins/` 新文件或 chat.py；群聊门 `rule=_cmd_gate`；权限用 `_is_admin` |
| 聊天上下文注入（像 assistant_state 那样） | chat.py `_do_chat`：并入 system_prompt（personas+address 后），**不要**单独追加中途 system 消息（部分服务商拒收） |
| 新面板页面/卡片 | `app/panel_page.html`（页面）+ `app/panel.py`（/api/*）；前端零依赖原生 JS |
| 新内置插件 | `app/plugin_manager.py` BUILTIN 加条目 + `app/plugins/<id>.py` + 运行时 `is_enabled` 门 |
| 新市场插件 | MARKET 加条目（pip 名真实存在 + module 显式）；type=local 表示"装依赖+开启" |
| 看图/图片输入 | `app/vision.py`（域名白名单/6MB/魔数）+ chat.py `_extract_images`/`_do_chat` |
| 模型调用 | `app/deepseek_api.py`（OpenAI 兼容，异常全翻译成中文）；聊天/看图/抖音决策共用 `.env` 的模型配置 |
| 配置项 | `.env.example` 同步新增 + `_cfg("KEY", "默认值")` 读取 |

## 3. 必须知道的坑（踩过，别重踩）

- 中文 Windows `netstat` 输出 GBK：subprocess 必须 `errors="replace"`
- NapCat 读**内层** `napcat/napcat/config/onebot11_<QQ>.json`，且要给所有历史账号写
- 反向 WS 客户端必带 `X-Self-ID` 头；OneBot 事件缺 `message_id` 被静默降级；回信走 `send_msg`
- 异步接口内禁止同步阻塞（曾致整面板假死）；文件 IO 用 `asyncio.to_thread`
- AsyncOpenAI 客户端不能跨事件循环复用——同步线程里每次 `asyncio.run` 须新建并 `aclose()`
- QQ 发送端风控：`Ban state forbit write req`=临时禁言；捕获 ActionFailed 记日志不崩
- `_restart_poll_delay` 曾自递归（测试 patch 函数本身掩盖了它）——测试禁止 patch 被测函数，只允许 patch 常量
- 测试曾误触真机（start 返回 True 因为手机真插着）——测试必须 mock connect 且 tearDown 恢复 `_state` 快照

## 4. 验证命令

```bat
.venv\Scripts\python.exe -m unittest discover tests -v      :: 89 用例
.venv\Scripts\python.exe -m ruff check app tools tests --select F,E9,ASYNC,B,UP041
.venv\Scripts\python.exe -m app.check_api                    :: 模型连通
```

## 5. 交付格式（每条功能/修复）

```
[ID] 名称
[状态] DONE/PARTIAL/SKIP
[改动] 文件清单
[实现] 思路 3-5 句
[测试] 新增用例名 + 全量 89 用例结果（真实输出）
[部署] 需要重启内核/面板？页面 F5 即生效？
[NOT RUN] 真机/真 QQ 验证项 + 手工步骤
```

最后：`git diff --stat`、风险与回滚方式（commit 号）。
