# Free API catalog

[Project](../README.en.md) · [Machine-readable catalog](free-api-catalog.json) · [简体中文](free-api-catalog.md)

A lightweight, easy-to-use personal API manager focused on aggregating free API access with your own accounts. No public keys are provided. Maintainers check official sources; the App setup assistant reads the latest GitHub catalog for each question. Offline access uses a clearly dated bundled snapshot. Catalog entries do not guarantee availability or your account's eligibility.

Last checked: **2026-09-06 UTC**.

| Provider | Free access | Setup and limits |
| --- | --- | --- |
| OpenRouter | Selected free variants, typically ending in `:free`; query current IDs | `https://openrouter.ai/api/v1`. Platform and upstream limits apply; account conditions affect allowances. [Free variants](https://openrouter.ai/docs/guides/routing/model-variants/free), [limits](https://openrouter.ai/docs/api_reference/limits) |
| Groq | Free Plan; examples `openai/gpt-oss-20b`, `openai/gpt-oss-120b` | `https://api.groq.com/openai/v1`. Organization/model request and token limits apply. [Free limits](https://console.groq.com/docs/rate-limits), [OpenAI compatibility](https://console.groq.com/docs/openai) |
| Google Gemini | Selected free-tier models; examples `gemini-2.5-flash`, `gemini-2.5-flash-lite` | `https://generativelanguage.googleapis.com/v1beta/openai`. Check AI Studio eligibility/region/limits and free-tier data policies. [Pricing](https://ai.google.dev/gemini-api/docs/pricing), [compatibility](https://ai.google.dev/gemini-api/docs/openai) |
| Cerebras | **Limited trial, not recurring free access**. Current docs describe $5 after payment-method verification, expiring in 30 days | `https://api.cerebras.ai/v1`. Purchased credits are needed after trial exhaustion/expiry. [Trial terms](https://inference-docs.cerebras.ai/support/rate-limits), [compatibility](https://inference-docs.cerebras.ai/resources/openai) |

Examples reflect the check date. Read the live model catalog and your account terms before testing. Model tests consume quota. Do not classify unknown pricing or trial credits as always-free access.

## App workflow

Initialize Mac local mode or connect/deploy your VPS. Add a Provider in its dedicated form, or replace a key for an existing built-in Provider. Keep keys out of assistant chat. Query models in Scan, confirm free terms, test a model, then add it to a task route and save. Start the gateway. The assistant uses the first available model in the configured thinking list; the catalog and built-in manual also work without a model.

## Maintenance

Report candidates through project Issues with official pricing/limit sources and model IDs. Never submit keys, private account screenshots, server addresses or personal usage. Maintainers update `free-api-catalog.json`, both human-readable pages and check dates. Distinguish recurring free tiers, trials and unverified entries; retire obsolete examples. GitHub catalog updates do not require an App update; bundled snapshots update with App releases.
