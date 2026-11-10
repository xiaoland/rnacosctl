# rnacosctl

面向 Agent 的 r-nacos 管理 CLI。通过控制台 API 管理配置、服务注册、用户、命名空间、备份和 MCP。首版兼容基线为 r-nacos **v0.8.8**。

## 安装

需要 Python 3.10 或更新版本。

```sh
uv tool install rnacosctl
# 或 pipx install rnacosctl
```

也可以从 GitHub 安装当前源码。

```sh
uv tool install git+https://github.com/xiaoland/rnacosctl.git
```

## 登录与切换

使用控制台地址。直连实例时，r-nacos 默认在 10848 提供控制台，反向代理通常使用 HTTPS。支持系统证书库，私有 CA 也可在首次登录时用 `--ca-bundle /path/to/ca.pem` 指定。非回环 HTTP 登录需要显式传入 `--allow-http`。

验证码关闭时，可以从终端安全输入密码，或让凭据工具把密码通过 stdin 传给 CLI。

```sh
rnacosctl --profile lab auth login --url https://config.example.org --username admin
# 由凭据工具提供密码时使用管道
your-secret-command | rnacosctl --profile lab auth login \
  --url https://config.example.org --password-stdin
```

验证码开启时，先取 PNG，由 Agent 的图像能力或操作者读取，然后用同一个挑战登录。挑战有效期为 5 分钟。以下示例由调用者设置 `CAPTCHA_ANSWER`。

```sh
rnacosctl --profile lab auth challenge \
  --url https://config.example.org --image challenge.png > challenge.json

your-secret-command | rnacosctl --profile lab auth login \
  --url https://config.example.org --password-stdin \
  --challenge-token "$(jq -r .data.challenge_token challenge.json)" \
  --captcha "$CAPTCHA_ANSWER"
```

`auth config` 可以查询实例的 OAuth2 设置。已经完成 OAuth2 授权的调用者可通过 `auth login --oauth-code-stdin` 提交授权码，或通过 `--token-stdin` 导入现有控制台会话。交互式授权由实例配置的身份提供方完成。

```sh
rnacosctl profile list
rnacosctl profile use lab
rnacosctl auth status
rnacosctl --profile another-lab namespace list
rnacosctl --profile lab auth logout
```

首次登录会选中该实例。之后可用 `profile use` 更改默认实例，也可用 `--profile` 或 `RNACOSCTL_PROFILE` 为单次调用选定目标。多个 Agent 并行使用时，分别显式指定目标即可。

CLI 保存服务器颁发的控制台会话，密码仅在登录进程中使用。会话继承实例的有效期，r-nacos v0.8.8 默认 24 小时。收到 `AUTH_REQUIRED` 时，重新登录同名 profile。会话文件独立于连接配置，Unix 上权限为 `0600`，Windows 上使用用户目录的继承权限。用 `RNACOSCTL_HOME` 可为独立任务指定自己的存储目录。

## Agent 调用约定

```sh
rnacosctl schema                       # 所有命令和参数
rnacosctl schema config set            # 仅查看一个命令
rnacosctl --profile lab namespace list
rnacosctl --profile lab namespace add --id myapp-dev --name 'My App Dev'
rnacosctl --profile lab config set --namespace myapp-dev --data-id app.json \
  --type json --file ./app.json
rnacosctl --profile lab config get --namespace myapp-dev --data-id app.json
rnacosctl --profile lab config export --namespace myapp-dev --output backup.zip
```

- 普通结果是 stdout 上的一行 JSON，结构为 `{"ok":true,"data":…}`。其中 `data` 使用服务器的字段名。命令帮助和版本使用文本，`schema` 可在离线和未登录时调用。
- 失败是 stderr 上的一行 JSON，读取 `error.code` 和可选的 `error.hint`。退出码 `1` 表示操作失败，`2` 表示输入或确认缺失，`3` 表示认证或权限问题。
- 支持流式输入的命令用 `--file -` 读 stdin。支持 `--output -` 的导出命令把原始内容写入 stdout。写到文件的导出不会覆盖已有文件。
- 删除、导入和回滚等操作要求 `--yes`。命令失败或超时时，先读取目标状态，再决定是否重试写操作。

| 命令组 | 能力 |
| --- | --- |
| `config` | 列表、读取、发布、删除、历史和恢复、按筛选或精确键导出 ZIP、导入 ZIP |
| `namespace` | 列表、创建、重命名和删除 |
| `user` | 当前用户、权限、用户列表、角色与命名空间授权、创建、更新、删除和改密 |
| `service`、`instance` | 服务和实例查询、创建、更新和删除，以及服务订阅者 |
| `cluster`、`metrics` | 集群成员、Leader 和指标时间线 |
| `tool-spec`、`mcp-server` | ToolSpec 管理、批量更新和 YAML 导入导出，MCP Server 管理、历史和发布 |
| `transfer` | 实例数据备份和恢复。备份文件可包含账户与会话数据，请按凭据文件保管 |

## 开发与发布

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
uv build
uv run twine check --strict dist/*
```

将 `RNACOSCTL_TEST_SERVER_BINARY` 指向本机的 r-nacos v0.8.8 可运行真实实例测试。测试使用临时数据目录和回环地址。服务端版本参见 [r-nacos v0.8.8](https://github.com/nacos-group/r-nacos/releases/tag/v0.8.8)。

版本标签采用 `v<version>`，对应 `pyproject.toml` 的版本。使用 PyPI API token 手动运行 `uv run twine upload dist/*`，再把同一份 wheel 和 sdist 附到 GitHub Release。
