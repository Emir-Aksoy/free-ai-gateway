# AI Gateway 管理

[前往下载最新 App](https://github.com/Emir-Aksoy/free-ai-gateway/releases/latest)

- `AI-Gateway-版本号-macos-arm64.dmg`：安装镜像，打开后拖入 Applications。
- `AI-Gateway-版本号-macos-arm64.zip`：压缩后的 `.app`，解压后拖入 Applications。
- `gateway-版本号.tar.gz`：App 首次部署所需的网关文件，解压后在向导中选择该目录。
- `SHA256SUMS.txt`：下载文件校验和。

当前支持 Apple Silicon（M 系列）Mac。App 不包含开发自检入口、个人连接记录或密钥；首次打开选择 Mac 本机或填写自己的 SSH 连接。v1.1 的本机运行组件随 App 提供；初始化后添加自己的 Provider 即可使用。详细部署步骤见仓库 README。

Provider 卡片支持删除：先预览调用链影响，确认后移除；空调用链会阻止删除。此功能要求 App 与 VPS 网关均为 v0.1.5 或更新版本。

v0.1.6 支持完整日志导出：在调用日志窗口按时间、Provider 或模型筛选后导出全部，或导出单条。文件保存在本机下载文件夹，包含升级后记录的完整内容，仅隐藏密钥；旧记录仍只有摘要。

v1.2 支持在 Provider 页双向同步 Mac / VPS 的服务商配置和密钥，先预览再确认。密钥只供网关使用，不提供显示或复制。在概览点击「汇总 VPS + Mac」查看同一 UTC 日与最近60秒的双端用量；这是按需快照，不是全局限流器。两端都须升级到 v1.2，完整性及计数说明见仓库 README。
