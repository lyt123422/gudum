# 第三方组件与许可

gudum 本身使用 MIT 许可证（见 `LICENSE`）。以下第三方组件由用户按需安装或随包分发，
其版权与许可证归原作者所有。

## WorkBuddy-Hub / workbuddy2api-hub

- 项目地址：<https://github.com/ardeyouxipianyi/workbuddy2api-hub>
- 用途：把 WorkBuddy 账号封装成本地 OpenAI 兼容接口，供 gudum 的「🔀 反代」功能使用。
- 许可证：MIT License（发布包内附 `LICENSE`）。
- 分发方式：gudum **源码仓库不包含**其源码或发布包。正式 Windows 发布包可以把官方
  Release 解压到 `vendor/WorkBuddy-Hub/` 一并分发（必须保留其 `LICENSE`）；从源码运行
  时，首次使用「登录 WorkBuddy」会由 `app/workbuddy_setup.py` 从官方 GitHub Release
  下载、校验 SHA256 后解压到 `data/workbuddy-hub/`。用户也可以使用已有安装或离线包。
- 版权声明：Copyright (c) 2026，具体以官方发布包内的 `LICENSE` 为准。
- 说明：gudum 只负责启动/停止该本地进程、健康检查和模型源切换，不修改其代码。
  使用 WorkBuddy 账号时请遵守其服务条款；账号、Key、额度均由用户自行提供。

如果以其他方式把 WorkBuddy-Hub 随 gudum 一起分发，请保留其 `LICENSE` 与本说明。
