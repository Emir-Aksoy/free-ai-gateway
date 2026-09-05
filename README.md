# free-ai-gateway

为个人使用各种模型 API 建立的 OpenAI 兼容聚合网关，目前主打**免费 API 的聚合调用**。使用自己的 VPS 和 Provider 账号，把不同服务商统一到一个 `/v1` 地址。

## 下载

- **[下载 macOS App 安装包](https://github.com/Emir-Aksoy/free-ai-gateway/releases/latest)**：适用于 Apple Silicon（M 系列）Mac。下载 DMG 后将「AI Gateway 管理」拖到 Applications；也提供 App ZIP。
- **[下载网关部署包](https://github.com/Emir-Aksoy/free-ai-gateway/releases/latest)**：选择 `gateway-版本号.tar.gz`，解压后供 App 安装向导使用。
- [免费大模型 API 雷达](https://emir-aksoy.github.io/free-ai-gateway/)：服务商政策、默认模型评分和公开资料。

发布页附 SHA256 校验和。App 暂无 Apple Developer ID 签名及公证；macOS 若阻止打开，请在确认下载来源与校验和后使用系统「隐私与安全性」中的允许打开。当前不提供 Intel Mac 或 Windows 安装包。

## 能做什么

- App 内确认 SSH 主机指纹，使用密码、私钥或 ssh-agent 登录并部署网关。
- 新增 OpenAI 兼容 Provider，查询免费模型目录，测试可用性并加入任务链。
- 拖动模型调用顺序，或按能力分排序；可分别启用延迟与成功率加分。
- 调整不同任务的模型评分，内置默认分；升级保留个人配置。
- 空文本、空白、仅 reasoning 而无最终回答均按失败降级；合法工具调用有效。
- 普通请求和流式请求均支持降级。流式开始输出后若发生错误，会报告失败，不拼接其他模型的回答。
- 创建独立的客户端密钥，供 OpenAI SDK 或兼容客户端调用。

## 用 App 首次部署

VPS 需 root、Python 3，以及 apt / systemd 兼容的 Linux 环境。

1. 安装 App，下载并解压网关部署包。
2. 在 App「连接」中填写自己的服务器地址、SSH 用户和端口，选择登录方式。
3. 核对 App 显示的 SHA256 主机指纹并确认。密码仅保留在本次 App 会话；退出即清除。
4. 点击「继续首次部署」，在安装向导选择刚解压的网关目录，完成环境检查、部署与启动。
5. 填写自己的 Provider 密钥，保存向导生成的客户端密钥；完整值只显示一次。
6. 进入「调用说明」，复制自己的地址和调用示例。

已部署的网关可直接连接管理。SSH 端口留空时沿用系统 SSH 配置；加密私钥请先加载到 ssh-agent。首次指纹扫描需要能直接访问 SSH 端口。

## 新增 Provider 与免费模型

在「Provider」页新增服务商，填写名称、OpenAI 兼容 Base URL、密钥环境变量名和密钥。密钥经 SSH 写到自己的 VPS。

在「扫描」页查询模型目录：查询只请求 `/models`，不发送生成请求。App 根据零价格元数据、已知免费模型标识或你明确登记的免费名单判断；任何非零价格证据优先于名单。计费未知时，先根据自己的账号套餐确认免费范围，再登记和测试。

测试可用后加入任务链。免费额度与模型目录由服务商决定，本项目不提供公共免费 API，也不承诺上游长期免费。

## 模型自动恢复

模型因多次失败被自动禁用后，网关会在 15 分钟后发送一个简短健康探测请求。失败后间隔延长到 30、60 分钟，之后每小时重试；时间进度在重启或跨天后保留。探测得到有效输出才会清除禁用和冷却，空文本或仅有思考内容仍算失败。

探测消耗 provider 配额，但不混入正常调用的成功率和延迟评分。额度不足时等待额度恢复。App 的模型页可查看下次探测时间；恢复不会改变你保存的模型顺序和评分策略。

## 调整顺序和评分

在「配置」页选择：

- **手动顺序**：按拖动后的顺序调用，跳过禁用、冷却或额度不足的候选。
- **评分排序**：使用能力分，可分别开启延迟与成功率加分；同分时保留拖动顺序。

支持 `fast`、`balanced`、`thinking`、`code`、`writing`、`agent` 六种任务。保存配置后生效。新安装默认手动顺序，旧配置未包含排序策略时保留原有自动评分行为。

## 调用 API

把 `<VPS_IP>` 换成自己的地址，`GATEWAY_KEY` 使用自己的客户端密钥。

```bash
curl http://<VPS_IP>:8090/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"balanced","messages":[{"role":"user","content":"你好"}]}'
```

OpenAI SDK 的 `base_url` 设置为 `http://<VPS_IP>:8090/v1`，`model` 填上述任务名；添加 `stream: true` 可使用流式输出。也可使用 `tools/ask.py`：

```bash
python3 tools/ask.py --base http://<VPS_IP>:8090 "你好"
```

网关默认提供 HTTP。公网使用时应配置 HTTPS 反向代理；仅供自己使用也可以通过 SSH 隧道连接。管理功能只走 SSH，网关没有 HTTP 管理接口。

## 命令行安装

在自己的 VPS 解压部署包后执行：

```bash
sudo bash install.sh
# 在 App 中连接该服务器，完成 Provider 密钥配置与启动。
```

`install.sh` 默认安装到 `/opt/ai-gateway`，监听端口默认 8090；通过 `GATEWAY_DIR`、`GATEWAY_PORT`、`GATEWAY_HOST` 和 `SERVICE_NAME` 可指定安装参数。安装器不覆盖已有配置和默认评分文件，也不自动启动服务。

## 仓库内容

| 文件 | 用途 |
| --- | --- |
| `manage.py`、`server.py`、`install.sh` | 管理、运行与安装网关 |
| `core/`、`router/`、`providers/` | 网关运行模块 |
| `config.yaml`、`defaults/` | 初始配置与默认评分 |
| `tools/ask.py`、`tools/probe.py` | 命令行调用与模型查询 |
| `docs/` | 公开资料页 |
| `app/` | App 下载说明；安装包位于 Releases |

公开发行内容不包含内部测试、开发记录、个人连接配置、密钥或运行数据。
