# AI Gateway 管理

[前往下载最新 App](https://github.com/Emir-Aksoy/free-ai-gateway/releases/latest)

- `AI-Gateway-版本号-macos-arm64.dmg`：安装镜像，打开后拖入 Applications。
- `AI-Gateway-版本号-macos-arm64.zip`：压缩后的 `.app`，解压后拖入 Applications。
- `gateway-版本号.tar.gz`：App 首次部署所需的网关文件，解压后在向导中选择该目录。
- `SHA256SUMS.txt`：下载文件校验和。

当前支持 Apple Silicon（M 系列）Mac。App 不包含开发自检入口、个人连接记录或密钥；首次打开由你填写自己的 SSH 连接。详细部署步骤见仓库 README。

Provider 卡片支持删除：先预览调用链影响，确认后移除；空调用链会阻止删除。此功能要求 App 与 VPS 网关均为 v0.1.5 或更新版本。
