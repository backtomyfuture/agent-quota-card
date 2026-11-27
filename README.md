# Agent Quota Card (`AI 限额看板`)

一款轻量级的 **macOS 原生桌面悬浮卡片 + 顶部菜单栏应用**（SwiftUI + Python），无需反复打开多个终端或进 TUI 敲 `/limits`、`/usage`，即可定时自动刷新并集中查看市面上主流 AI Coding Agent 的剩余额度与重置倒计时。

---

## ✨ 特性

- **桌面毛玻璃悬浮小卡片 + 菜单栏联动**
  - 原生 `swiftc` 编译，无 Electron/浏览器内存开销，`LSUIElement` 无 Dock 图标干扰。
  - 支持自由拖拽并记住位置，支持 **始终置顶 (`📌`) / 普通层级** 一键切换，支持折叠为紧凑摘要视图 (`⌃/⌄`)。
  - 顶部菜单栏实时展示剩余额度简报，左键快速显隐/前置（适配 Stage Manager 台前调度），右键提供快捷菜单。
- **内置智能体选择配置面板 (`⚙️`)**
  - 打开卡片内配置面板即可自由勾选要监控的 AI 工具，支持 **「自动检测本机已安装/已登录」**、**「全选」**、**「仅核心 4 个」** 一键筛选。
  - 可自定义自动刷新周期（`5 / 10 / 15 / 30 / 60 分钟`）。
- **零 TUI 模拟，直接对接本地凭据与官方接口**
  - 全程 100% 本地运行，直接复用各 CLI 工具本机已登录的 Keychain / 本地凭据文件请求对应配额接口（并支持 OAuth Token 自动刷新），不上传任何数据到第三方服务器。

---

## 🤖 支持的 AI 智能体（15 款）

| ID | 名称 | CLI | 厂商 | 配额获取方式 |
| :--- | :--- | :--- | :--- | :--- |
| `agy` | **Antigravity** | `agy` | Google | 优先读取本机 `LanguageServerService/RetrieveUserQuotaSummary`（Gemini 5h/Weekly + Claude/GPT Weekly）；离线时自动回退云端接口与 Keychain OAuth 刷新 |
| `droid` | **Factory Droid** | `droid` | Factory | 读取 Keychain + AES-256-GCM 解密 `~/.factory/auth.v2.loginkeychain`，直连 `/api/billing/limits`（5h / Weekly / Monthly / Core / Extra Balance，支持 WorkOS 刷新） |
| `grok` | **Grok Build** | `grok` | xAI | 读取 `~/.grok/auth.json`，直连 `cli-chat-proxy.grok.com/v1/billing?format=credits`（Weekly/Monthly + On-Demand/Prepaid，支持 OIDC 刷新） |
| `amp` | **Amp** | `amp` | Sourcegraph | 调用 `amp usage` 并解析 Amp Free 日限额与 Workspace 余额 |
| `codex` | **OpenAI Codex** | `codex` | OpenAI | 读取 `~/.codex/auth.json`，直连 `chatgpt.com/backend-api/wham/usage`（Rate Limit 窗口、Code Review 与 Credits，支持 OAuth 刷新） |
| `claude` | **Claude Code** | `claude` | Anthropic | 读取 Keychain (`Claude Code-credentials`) 或 `~/.claude/.credentials.json`，调用 `api.anthropic.com/api/oauth/usage`（5h / 7d 利用率） |
| `copilot` | **GitHub Copilot** | `copilot` | GitHub | 复用 `gh auth token` 或本地 Copilot OAuth 配置，调用 `api.github.com/copilot_internal/user` 解析 `quota_snapshots` |
| `kimi` | **Kimi Code** | `kimi` | Moonshot | 读取 `~/.kimi-code/credentials/` 或 `KIMI_API_KEY`，调用 `api.kimi.com/coding/v1/usages` |
| `gemini` | **Gemini CLI** | `gemini` | Google | 读取 `~/.gemini/oauth_creds.json`，调用 `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` |
| `cursor` | **Cursor** | `cursor` | Anysphere | 读取 Cursor 本地 `state.vscdb` 中的登录态，调用 `cursor.com/api/usage-summary` |
| `freebuff` | **Freebuff** | `freebuff` | Codebuff | 读取 Keychain (`freebuff-cli`) 或 `CODEBUFF_API_KEY`，调用 `www.codebuff.com/api/v1/usage` |
| `kiro` | **Amazon Kiro** | `kiro` | AWS | 读取 `kiro-cli/data.sqlite3`（支持 OIDC 自动刷新），调用 `q.<region>.amazonaws.com/getUsageLimits` |
| `glm` | **GLM Coding Plan** | `glm` | Zhipu / Z.AI | 读取 `Z_AI_API_KEY`、`~/.config/glm-acp-agent/credentials.json` 或 `~/.claude/settings.json`，调用 `/api/monitor/usage/quota/limit` |
| `deepseek` | **DeepSeek** | `deepseek` | DeepSeek | 读取 `DEEPSEEK_API_KEY` 或 OpenCode/本地配置，调用 `api.deepseek.com/user/balance` |
| `openrouter` | **OpenRouter** | `openrouter` | OpenRouter | 读取 `OPENROUTER_API_KEY` 或 OpenCode/本地配置，调用 `openrouter.ai/api/v1/credits` |

> **说明（为何暂不包含部分消费端/强加密桌面应用）：**
> - **Muse (`com.meta.endo`) / 豆包工作 (`DoubaoWork`)**：消费级 / Chromium WebView 套壳应用，未暴露可查询的用户额度接口。
> - **Grok Bot (`com.anysphere.sand`) / WorkBuddy & WorkBuddyPro**：本地凭据分别使用 Electron `safeStorage`（macOS 钥匙串强权限隔离）或 `$wbEncrypted` AES-256-GCM 封装；出于安全与隐私合规原则，本项目不绕过系统钥匙串权限或解密应用专属保险库。

---

## 🛠️ 环境要求与安装构建

### 依赖要求
- macOS 13.0+
- Xcode Command Line Tools（系统自带 `swiftc` 与 `codesign`）
- Python 3（仅使用标准库）
- Node.js（仅在启用 `droid` 且需要 AES-256-GCM 解密 `~/.factory/auth.v2.*` 时使用）

### 一键编译安装
```bash
git clone https://github.com/backtomyfuture/agent-quota-card.git
cd agent-quota-card
chmod +x build.sh
./build.sh
open /Applications/QuotaCard.app
```

> 如需构建到其他目录，可传入 `APP_DIR=./dist ./build.sh`。

### 开机自启动
打开 **系统设置 → 通用 → 登录项**，将 `/Applications/QuotaCard.app` 添加进去即可。

---

## 🧪 单独使用命令行采集器

采集脚本 [collector/quota_collector.py](collector/quota_collector.py) 也可单独输出 JSON，方便接入 Raycast、SwiftBar 或其他脚本：

```bash
# 查看所有支持的智能体及本机检测状态
python3 collector/quota_collector.py --catalog

# 自动采集本机已检出的全部智能体
python3 collector/quota_collector.py

# 仅采集指定智能体
python3 collector/quota_collector.py --providers agy,droid,grok,amp,codex,copilot

# 强制采集全部 15 个智能体
python3 collector/quota_collector.py --all
```

---

## 🔒 隐私与安全

- 所有凭据仅从你本机的钥匙串（Keychain）或各自 CLI 的标准配置目录读取，仅用于向对应的官方 API 端点查询配额。
- 请求通过系统 `/usr/bin/curl -K -`（标准输入配置）发送，**Token 不会暴露在进程命令行参数中**。
- 本项目不包含任何统计上报或远端服务器。

## 📄 License

[MIT](LICENSE)
