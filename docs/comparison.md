# 项目定位与取舍 / Positioning

free-ai-gateway 是**轻量、易用的个人 API 管理工具**，优先服务“在自己的 Mac / VPS 上管理多个免费 API 账号”的场景。精简助手连接公开目录、内置操作手册与模型解读；具体配置仍由用户确认操作。

| 项目 | 擅长的场景 | 与本项目的取舍 |
| --- | --- | --- |
| 本项目 | Mac App、SSH部署管理、免费目录与文字指引、模型测试、任务排序、双端Provider同步 | 个人使用流程集中；协议/Provider覆盖、分布式硬限流、多租户治理及长期生产验证相对有限 |
| [LiteLLM](https://docs.litellm.ai/docs/) | 广泛Provider适配、Python SDK、团队预算与观测集成 | 更适合平台集成；本项目聚焦个人桌面使用。企业功能需与基础能力区分 |
| [New API](https://github.com/QuantumNous/new-api) | 多用户、渠道、额度核算、多协议管理 | 组织管理能力更全面；本项目没有同等核算与多用户平台能力 |
| [Bifrost](https://docs.getbifrost.ai/quickstart/gateway/setting-up) | 并发性能、本机运行、可视化Provider管理与MCP生态 | 与轻量本机目标直接重叠；未经同机测试不能断言本项目更快或更省资源 |
| [OpenRouter](https://openrouter.ai/docs/guides/overview/models) | 托管目录与统一接入，无需自建网关 | 更省服务维护；受平台目录/政策约束，也可作为本项目其中一个Provider |

内置助手增加的是“理解免费条件并完成配置”的便利性，不会自动提高上游模型质量或网关协议兼容程度。此对比依据官方文档（2026-09-06核查），没有进行跨项目同机性能基准，不作吞吐/内存优胜声明。

In English: this project focuses on an approachable personal Mac/VPS workflow for managing your own API accounts, especially free tiers. LiteLLM offers broad SDK/platform integrations; New API emphasizes user/channel/accounting management; Bifrost overlaps with lightweight local operation and performance; OpenRouter is a hosted option and can also be used as one Provider here. The setup assistant improves guidance, not upstream model quality or protocol coverage. No comparative performance benchmark has been run.
