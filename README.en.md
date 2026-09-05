# free-ai-gateway

English · [简体中文](README.md)

A personal, self-hosted gateway for using your own model API accounts. The project currently focuses on aggregating **free API access** behind one OpenAI-compatible `/v1` endpoint. It does not provide public API keys or guarantee upstream availability or free pricing.

## Download and install

Download the Apple Silicon macOS App and gateway archive from [GitHub Releases](https://github.com/Emir-Aksoy/free-ai-gateway/releases/latest). Release assets include a DMG, App ZIP, gateway tar.gz and SHA256 checksums. The App currently has no Apple Developer ID signature/notarization. Verify its source and checksum before allowing it in macOS Privacy & Security.

1. Install the App and extract the gateway archive.
2. Enter your VPS SSH connection in the App. Verify its SHA256 host fingerprint before accepting it.
3. Sign in using a password, private key or ssh-agent. Passwords remain in the current App session only.
4. Choose first deployment, select the extracted gateway directory, and run the environment checks and installation.
5. Add your Provider credentials and save the generated client key when it is shown.
6. Open the usage page for your API address and examples. Use the language selector to switch between English and Simplified Chinese; the choice is saved locally.

First deployment requires root access and a Linux VPS compatible with Python 3, apt and systemd. Management uses SSH; there is no HTTP administration endpoint. The API uses HTTP by default. Configure an HTTPS reverse proxy for public access, or use an SSH tunnel for personal access.

## Providers and free models

Add an OpenAI-compatible Provider with its base URL and your own API key. Model discovery requests `/models` without generating a completion. Free status uses zero-price metadata, known free identifiers, or your explicitly registered free-model list. Nonzero pricing evidence takes precedence. Confirm unknown pricing against your account before testing models.

Provider deletion previews affected routes and scores and prevents empty routes. Dedicated credential files are privately backed up before deletion; shared and external credential files are preserved. Historical logs and usage remain available.

## Routing and reliability in v1.0

Use `fast`, `balanced`, `thinking`, `code`, `writing` or `agent` as the requested model. These are task aliases; the selected upstream model supplies the response.

Each task may inherit the default policy or use its own:

- **Manual order:** try models in your drag-and-drop order, skipping unavailable candidates.
- **Score order:** rank by capability, with optional reliability and first-output latency adjustments. Ties retain the configured order. The legacy fallback list remains at the end in score mode.
- **Preferred first:** try the first configured model, then rank the remaining candidates by score.

Task metrics retain at most 100 business samples per model/task over the last 24 hours, separately from daily counters. Reliability uses a smoothed success rate. Streaming first useful output, total duration and output throughput are separate metrics. Total answer duration is not substituted for first-output latency; missing or insufficient latency samples receive a neutral adjustment. Health probes do not alter business scores.

Credential/permission errors temporarily pause the Provider; rate limits or insufficient balance wait before retrying. Request/model incompatibility remains a business failure but does not count toward reliability-based model disabling. Empty output, interrupted streams, timeouts, network errors and service errors do count. Valid tool calls are accepted. Client cancellation does not count as model failure.

Disabled models receive independent basic health probes after 15, 30 and 60 minutes, then hourly. A basic recovery does not guarantee long-context, streaming or tool-call support. A successful manual model test can clear a Provider pause; model recovery remains separate.

## Logs and route explanations

Fetch logs on demand by time, Provider or Provider/model, with optional source and outcome filters. Each page is limited to 50 records, with no polling.

Open a request's routing history to see all matching attempts across models, up to 50 per page. It shows the requested task, policy, candidate score components, recent sample counts and skip reasons. Scores are a snapshot from the start of the request; the actual attempt records show its outcome. Old logs cannot reconstruct previously unrecorded information.

Export one record or all retained records matching the filters as JSONL to your local Downloads folder. Exports include complete request bodies, upstream attempts, responses, stream fragments and original errors, with credentials redacted before storage and again during export. Only a successfully completed export produces a final file, readable/writable by the current user.

Logs stay on your VPS and are not uploaded to GitHub. Retention is up to 50,000 records and 512 MiB of compressed detail, removing oldest whole records and retaining at least the latest one. Retained bodies are not truncated. Old summary-only records cannot recover missing bodies.

## API example

Replace `<YOUR_HOST>` with your own address and set `GATEWAY_KEY` to your client key:

```bash
curl http://<YOUR_HOST>:8090/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"thinking","messages":[{"role":"user","content":"Explain your approach."}],"stream":true}'
```

Use the same `/v1` base URL in an OpenAI-compatible client. Streaming can fall back before useful output begins; errors after output begins fail the stream instead of mixing another model's answer into it.

## Distribution

The public repository contains only user-facing runtime files, defaults and documentation. App installers are published in Releases. Internal tests, App development sources, development records, credentials, server addresses and private runtime data are excluded from the public distribution.
