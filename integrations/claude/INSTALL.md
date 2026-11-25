# 安装 QueryGate Claude 插件

准备一个这台电脑能访问的 **QueryGate REST 地址**和一个已授权的数据源客户端 token。地址填写服务根路径，例如 `https://querygate.example.com`。默认的 `http://127.0.0.1:8000` 指向当前电脑。本机以外的服务请使用 HTTPS。

在 Windows 自行启动服务时，在服务端目录运行 `.\Start-QueryGate.ps1 --configure` 或 `.\Start-QueryGate.cmd --configure`。在打开的 Web 管理页保存只读数据源，设置表和视图的访问规则，并从「授权客户端」复制客户端 token。保持服务运行，同一台电脑上的插件可使用默认地址和该 token。

## Claude Desktop

1. 打开设置，进入 **Extensions → Advanced settings → Install Extension…**。
2. 选择 `QueryGate-Claude-Desktop-0.1.0.mcpb`。
3. 在扩展配置中填写 **QueryGate URL** 和 **QueryGate client token**。扩展需要 Node.js 20.3+，JavaScript 依赖已随包提供。

## Claude Code

需要支持插件配置的 Claude Code 及可从 `PATH` 启动的 Node.js 20.3+。解压 `QueryGate-Claude-Code-0.1.0.zip` 后，从解压位置运行以下命令。

```text
claude plugin marketplace add ./querygate-claude-code
claude plugin install querygate@querygate-local
```

打开 Claude Code，在 `/plugin` 中完成 QueryGate 的 URL 和 token 配置。通过 `/mcp` 查看 QueryGate 工具状态。

## 第一次使用

服务端与插件请同步升级到 v0.2.0。在新的对话中输入「用 QueryGate 列出我可访问的数据源」。`get_schema` 默认每页列出 50 个授权对象，并区分表和视图。查询和写入都需要 Agent 填写业务原因。具有写入权限的客户端可用 `submit_mutation` 提交 UPDATE／INSERT，请求状态可用 `get_mutation` 查询。

- `GATEWAY_UNAVAILABLE` 表示插件无法访问配置的 REST 服务。检查服务是否启动、HTTPS 证书以及另一台电脑的网络连通性。
- `UNAUTHENTICATED` 表示服务拒绝了 token。检查 token 是否属于该 QueryGate 实例。
- 数据源可列出时，授权目录和实际查询仍由服务器端配置与数据库权限决定。

两个包均只安装 Claude 侧的 MCP 客户端组件。数据库连接与凭证由 QueryGate REST 服务管理。服务端包中的 `README.md` 提供启动说明。[QueryGate 项目](https://github.com/EasyingFreeman/QueryGate)
