# GUIDE-PAGE.md · 面板「📖 使用教程」页面片段

> 以下 HTML 可整体替换 `app/panel_page.html` 中 `<!-- 使用教程 -->` 到 `<!-- 运行控制 -->` 之间的内容。
> 只用现有 class（card / step / pl-item / tip / desc / row / button），不含 JS/API。
> 同步版本文案见 TUTORIAL.md（v4 · 2026-10-25）。

```html
<!-- 使用教程 -->
<div class="page" id="pg-guide">
  <div class="card">
    <h3>⚡ 第一次使用（约 10–20 分钟，以后每次 1 分钟）</h3>
    <div class="desc">按顺序做完四步就能聊；卡住看本页底部排障表</div>
    <div class="step"><div class="n">1</div><div><b>配模型</b> —— 🧠 模型配置 → 点提供商胶囊 → 填 API Key → <b>🔌 测试连通</b>出现 ✅ → <b>💾 保存并重启机器人</b>（只聊天用文本模型即可）</div></div>
    <div class="step"><div class="n">2</div><div><b>连 QQ</b> —— 🚀 运行控制 → <b>▶ 启动</b>内核 → <b>▶ 启动</b>协议端 → 用<b>小号</b>扫二维码（约 2 分钟有效，过期点 🔄 刷新二维码）→ 手机点授权</div></div>
    <div class="step"><div class="n">3</div><div><b>认领管理员</b> —— 用主号私聊机器人：<b>我是主人 认领码</b>（认领码见下方认领码提示；10 分钟有效）→ 收到「✅ 你已成为管理员」。不认领：换人设/看状态/抖音都用不了</div></div>
    <div class="step"><div class="n">4</div><div><b>开聊</b> —— 主号加小号好友，私聊直接发消息；群里 <b>@机器人</b> 或 <b>/命令</b>；发 <b>帮助</b> 看完整菜单</div></div>
    <div class="tip">⚠ 只用小号（协议端有风控风险）；数据都在本机；面板不要暴露到公网。</div>
  </div>

  <div class="card">
    <h3>⌨️ 指令速查（群聊需 @ 或 / 前缀）</h3>
    <div class="pl-item"><div><div class="nm">所有人 · 聊天</div><div class="ds">直接发文字，记忆 2 小时；发「重置」清空；问新闻/天气自动联网，或「联网 问题」强制</div></div></div>
    <div class="pl-item"><div><div class="nm">所有人 · 人设</div><div class="ds">「人设列表」看库 · 「叫我 XX」设定称呼 · 人设切换与管理员在下面</div></div></div>
    <div class="pl-item"><div><div class="nm">所有人 · 语音</div><div class="ds">「语音开 / 语音关」—— 回复附带角色配音（人设可指定音色）</div></div></div>
    <div class="pl-item"><div><div class="nm">所有人 · 表情</div><div class="ds">回复图片发「存表情 名字」收录 ·「表情包 [词]」斗图 ·「表情列表」</div></div></div>
    <div class="pl-item"><div><div class="nm">👑 管理员 · 人设</div><div class="ds">「切换人设 名字」换人格 ·「人设 描述」改性格 ·「状态」看运行信息</div></div></div>
    <div class="pl-item"><div><div class="nm">👑 管理员 · 管理员</div><div class="ds">「我是主人 认领码」首次认领 ·「设置管理员 QQ」加人</div></div></div>
  </div>

  <div class="card">
    <h3>🧩 插件</h3>
    <div class="desc">🧩 插件页：上半是内置开关（即点即生效），下半是市场（点 ⬇ 自动装依赖）</div>
    <div class="tip">· 联网 / 语音 / 表情包：默认开，零配置<br>
      · 抖音真机：市场安装 → 手机开 USB 调试连电脑 → QQ 发「连接手机」<br>
      · 第三方插件装完内核自动重启（QQ 不受影响）；内核没开则提示"下次启动生效"<br>
      · ⚠ 对话类第三方插件会和内置聊天抢消息，二选一</div>
  </div>

  <div class="card">
    <h3>🖼 看图（可选，需多模态模型）</h3>
    <div class="desc">默认关闭。开启三步：① 换多模态模型（qwen-vl / glm-4v / gpt-4o / gemini 等）② 记事本改 .env：VISION_ENABLED=true ③ 重启内核</div>
    <div class="tip">然后私聊/群@ 直接发图就能聊；图片在本机下载校验后发给模型。文本模型发图会提示"不支持看图"。</div>
  </div>

  <div class="card">
    <h3>🎬 抖音真机（可选，管理员，需多模态模型）</h3>
    <div class="desc">AI 看手机屏幕两帧画面决定 多看/点赞/关注/划走——真滑动、真声音（手机播放）。AI 只看截图帧，没有声音，问内容措辞以画面为准</div>
    <div class="tip">① 市场安装「抖音真机」② 手机开 USB 调试连电脑、装好抖音、保持解锁亮屏 ③ QQ 发「连接手机」诊断 →「刷抖音 分钟」<br>
    ⚠ 单次 ≤60 分钟、每日 ≤120 分钟；违反平台协议有风控风险，仅限小号自用；"AI 连续失败"= 模型看不了图</div>
  </div>

  <div class="card">
    <h3>🩺 不回话？三步排查</h3>
    <div class="step"><div class="n">1</div><div><b>Key 灯</b>没绿 → 模型配置测试连通：401=Key 错 · 402=余额不足 · 404=模型名错</div></div>
    <div class="step"><div class="n">2</div><div><b>QQ 灯</b>没绿 → 重启协议端刷新二维码重扫；互踢=两个程序登同一号</div></div>
    <div class="step"><div class="n">3</div><div>都绿不回 → 看 📜 运行日志末尾；"发送被QQ拦截"=临时风控，等几分钟自动恢复</div></div>
    <div class="tip">进阶排障/数据备份/费用说明见项目 TUTORIAL.md 第 12–13 节。</div>
  </div>

  <div class="card">
    <h3>💬 交流与反馈</h3>
    <div class="desc">教程没覆盖的问题，进频道问</div>
    <div class="row"><button onclick="window.open('https://pd.qq.com/s/7hbug91t7?b=5','_blank')">💬 加入 gudum 交流频道</button></div>
  </div>
</div>

<!-- 运行控制 -->
```
