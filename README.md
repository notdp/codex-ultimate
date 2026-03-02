# codex-ultimate

OpenAI Codex 账号池自动维护工具。自动清理失效账号（401）、按需补号注册、验证码收取，保持账号池在目标数量以上。

## 前置依赖

- [Node.js](https://nodejs.org/) >= 18
- [Python](https://www.python.org/) >= 3.10
- [pipx](https://pipx.pypa.io/)（`brew install pipx` 或 `pip install pipx`）
- 运行中的 CLIProxyAPI 后端实例

## 快速开始

```bash
# 一键安装 + 交互式配置
npx github:notdp/codex-ultimate install

# 前台运行
npx github:notdp/codex-ultimate run

# 后台运行
npx github:notdp/codex-ultimate start

# 查看状态
npx github:notdp/codex-ultimate status

# 停止后台进程
npx github:notdp/codex-ultimate stop
```

`install` 会自动通过 pipx 安装 Python 包到隔离环境，然后引导你填写配置。

## 配置

配置文件路径：`~/.codex-ultimate/config.json`

由 `install` 向导自动生成，也可以手动编辑。完整字段参考 [config.json.example](./config.json.example)。

### 核心配置项

| 字段 | 说明 |
|---|---|
| `clean.base_url` | CPA 后端地址 |
| `clean.token` | CPA 认证 Token |
| `email.provider` | 邮箱提供者：`mail_gateway` 或 `icloud` |
| `email.email_domains` | Catch-All 域名列表，注册时随机生成 `xxx@域名` |
| `mail_gateway.base_url` | Mail Gateway API 地址 |
| `mail_gateway.token` | Mail Gateway API Token |
| `icloud.username` | iCloud 账号（选 icloud 时需要） |
| `icloud.app_password` | iCloud App 专用密码 |
| `run.workers` | 并发注册数 |
| `run.proxy` | 代理地址，留空不使用 |
| `maintainer.min_candidates` | 最小候选账号数，低于此值自动补号 |

## 运行流程

1. **清理**：探测池中所有账号，删除已失效的（401）
2. **统计**：检查当前 candidates 数量是否达标
3. **补号**：不足则自动注册新账号，通过 Mail Gateway 或 iCloud IMAP 收取验证码，完成 OAuth 认证后上传至 CPA

## 日志

日志目录：`~/.codex-ultimate/logs/`

## 额外参数

`run` 和 `start` 支持追加参数：

```bash
npx github:notdp/codex-ultimate run --min-candidates 20
npx github:notdp/codex-ultimate start --min-candidates 20 --timeout 30
```

## License

ISC
