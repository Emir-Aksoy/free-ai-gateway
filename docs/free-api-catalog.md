# 免费 API 目录

[项目主页](../README.md) · [机器可读目录](free-api-catalog.json) · [English](free-api-catalog.en.md)

本项目是**轻量、易用的个人 API 管理工具**，目前侧重免费 API 聚合。使用你自己的服务商账号，不提供公共密钥。此目录由维护者核对官方资料后更新，App 接入助手每次提问会读取 GitHub 上的最新版；不可达时显示内置快照及日期。目录不是实时可用性保证，也不代表你的账号一定具有相同额度。

最近核查：**2026-09-06 UTC**。

| Provider | 免费类型与示例 | 接入与限制 |
| --- | --- | --- |
| OpenRouter | 部分免费变体，通常带 `:free`；模型名单会变化，请在 App 查询 | Base URL `https://openrouter.ai/api/v1`。平台与上游均可限流，账号条件影响额度。[免费说明](https://openrouter.ai/docs/guides/routing/model-variants/free)、[限制](https://openrouter.ai/docs/api_reference/limits) |
| Groq | Free Plan；示例 `openai/gpt-oss-20b`、`openai/gpt-oss-120b` | Base URL `https://api.groq.com/openai/v1`。按组织与模型限制请求/Token，任一上限先用尽都会限流。[免费限额表](https://console.groq.com/docs/rate-limits)、[兼容接口](https://console.groq.com/docs/openai) |
| Google Gemini | 部分模型免费层；示例 `gemini-2.5-flash`、`gemini-2.5-flash-lite` | Base URL `https://generativelanguage.googleapis.com/v1beta/openai`。资格、地区和限额看 AI Studio；免费层数据使用政策可能不同。[定价](https://ai.google.dev/gemini-api/docs/pricing)、[兼容接口](https://ai.google.dev/gemini-api/docs/openai) |
| Cerebras | **仅限时试用，不是持续免费**。当前官方说明为验证付款方式后赠5美元，30天到期 | Base URL `https://api.cerebras.ai/v1`。用完/到期后需购买额度；保留此行用于提醒政策变化。[试用政策](https://inference-docs.cerebras.ai/support/rate-limits)、[兼容接口](https://inference-docs.cerebras.ai/resources/openai) |

示例模型只说明核查时官方资料列出过该模型。先查询当前 `/models`、账号资格和免费条件，再测试；测试消耗额度。没有明确免费定价的模型不能凭名字或助手判断为免费，试用赠金也不应登记为永久免费。

## 在 App 中使用

1. 首次使用选择 Mac 本机初始化，或连接/部署自己的 VPS。
2. Provider 页新增服务商，或为已存在的内置服务商更换密钥。密钥只填在专门表单，不填助手对话。
3. 扫描页查询目录；根据官方证据确认免费模型，必要时维护 Provider 的免费模型列表。
4. 由用户点击测试模型，检查完整返回、状态、耗时。查询目录不是生成测试。
5. 配置页把验证后的模型加入 thinking 等任务链，拖动顺序并保存，启动网关。接入助手使用 thinking 中首个可用模型；无模型时仍可读目录和内置手册。

## 维护方式

欢迎通过项目 Issue 提供官方定价/限制链接和 model ID，不提交 key、账号截图、服务器地址或个人运行统计。仅凭社区“免费”说法不收录为已核实免费。

维护者修改 `docs/free-api-catalog.json`，同步更新本页与英文页，并更新 `checked_at` / `updated_at`。明确区分持续免费额度、试用与待核查，移除过期模型示例。App 的内置快照随版本发布更新，GitHub 目录可独立更新；不得把来源无法确认的条目标成刚刚核实。
