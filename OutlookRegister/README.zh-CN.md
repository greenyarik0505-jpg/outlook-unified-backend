<h1 align="center">OutlookRegister</h1>

<p align="center">
  Outlook / Hotmail 自动注册，并获取 Microsoft Graph OAuth2 <code>refresh_token</code>（基于 patchright 浏览器自动化）。
</p>

<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README.zh-CN.md"><b>简体中文</b></a> ·
  <a href="./README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="patchright" src="https://img.shields.io/badge/Browser-patchright-4B5563">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue">
</p>

## 配套项目

- **[Easy Proxies](https://github.com/daimon3332/easy-proxies)（强烈推荐搭配使用！！！！！！）— 多端口模式必需。** 当 `proxy.mode` 设为 `multiple` 时，为 OutlookRegister 提供本地代理池；使用该模式前请先启动并配置 Easy Proxies。
- **[OutlookManage](https://github.com/daimon3332/OutlookManage) — 账号管理。** 用于导入和管理已注册的 Outlook 账号及其 OAuth2 凭据。
- **[Outlook OAuth GetToken](https://github.com/daimon3332/Outlook-Oauth-GetToken) — 单独获取令牌。** 无需运行注册流程，即可为已有微软邮箱获取 OAuth2 `refresh_token`。

> 本项目基于 **[LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister)** 二开。  
> 增强点包括：可选辅助邮箱绑定、更完整的 OAuth 中间页处理、cookie 优先授权、分批调度，以及 Ctrl+C 中断后的汇总与清理。

---

## 使用教程

### 1. 环境要求

- 建议 Python 3.10+  
- 可用的 HTTP/SOCKS 代理（强烈建议）  
- 若开启辅助邮箱绑定：兼容的临时邮箱 / CF Temp Mail 类 API  

### 2. 安装

```bash
git clone <本仓库地址>
cd OutlookRegister
pip install -r requirements.txt
patchright install chromium
```

### 3. 配置

```bash
# Windows
copy config.example.json config.json

# Linux / macOS
cp config.example.json config.json
```

编辑 `config.json`：至少填好 **proxy**。对外分享时不要带上真实密钥。

### 4. 运行

```bash
python main.py
```

成功账号会**追加**写入 `Results/oauth2.txt`：

```text
邮箱----密码----client_id----refresh_token
```

日志在 `log/`。按 **Ctrl+C** 可中断：先写汇总，再关闭浏览器并清理 profile。

---

## `config.json` 字段说明

模板见 `config.example.json`（与空的 `config.json` 结构一致）。

### 顶层字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `email_suffix` | string | 注册邮箱后缀，如 `@outlook.com` 或 `@hotmail.com`。 |
| `headless` | bool | `false` 显示浏览器窗口；`true` 无头。 |
| `bot_protection_wait` | number | 填表节奏基准（秒）。代码内会 ×1000 作为等待。 |
| `max_captcha_retries` | number | 验证码按压额外重试次数（约 `max_captcha_retries + 1` 轮 Hold）。 |
| `captcha_strategy` | number | 验证码/交接策略，见下表。 |
| `concurrent_flows` | number | 并发线程数（同时打开的浏览器任务数）。 |
| `tasks` | number | 全局提交任务上限；与 `success_tasks` **任一达标**即结束。 |
| `success_tasks` | number \| null | 全局成功上限。`null` = 不按成功数截断（仍受 `tasks` 限制）。 |
| `batch_success_limit` | number | 单批成功数上限；达到后重置程序内代理权重/统计并开下一批。累计成功/耗时保留。**不会**更换固定代理的真实出口 IP。 |
| `proxy` | object | 代理配置（正式使用必填）。 |
| `oauth2` | object | Graph OAuth2 配置。 |
| `temp_mail` | object | 可选：保护帐户页自动绑定辅助邮箱。 |

### `captcha_strategy`

| 值 | 行为 |
| --- | --- |
| `0` | 全自动（验证码 + 进邮箱 + OAuth）。 |
| `1` | 半自动：你手动过验证码，其余自动。 |
| `2` | 验证码界面出现后交给人工；该任务程序不跑 OAuth。 |

### `proxy`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `mode` | string | `single` 使用 `single_port`；`multiple` 使用 `port_start`～`port_end` 端口池。 |
| `type` | string | 代理协议，如 `http`、`socks5`。 |
| `host` | string | 代理主机，如 `127.0.0.1`。**运行前请填写。** |
| `single_port` | number | `mode=single` 时的端口。 |
| `port_start` | number | `mode=multiple` 时起始端口。 |
| `port_end` | number | `mode=multiple` 时结束端口（含）。 |
| `max_per_proxy` | number | 单个端口在进程内最多被选中次数；用满后暂不选，全满或批次重置后计数清零。 |

### `oauth2`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `enable_oauth2` | bool | `false` 时注册成功即可计成功，不拉 token。 |
| `client_id` | string | 授权与换 token 使用的客户端 ID。 |
| `redirect_url` | string | 重定向 URI（默认 `http://localhost`）。 |
| `Scopes` | string[] | 授权范围，一般为 `offline_access` + Graph 默认范围。 |

### `temp_mail`

仅在微软弹出 **「让我们来保护你的帐户」** 且开启绑定时使用。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `enabled` | bool | `false` = 不自动绑定（出现保护页时走跳过等逻辑）。 |
| `base_url` | string | 临时邮箱 API 根地址。不用则留空。 |
| `admin_password` | string | 创建地址用的管理员密码。**勿公开。** |
| `domain` | string | 新建邮箱域名。 |
| `name_prefix` | string | 本地部分前缀（可选）。 |
| `enable_prefix` | bool | 是否启用 `name_prefix`。 |
| `code_timeout` | number | 等待验证码邮件的超时（秒）。 |
| `poll_interval` | number | 轮询收件箱间隔（秒）。 |

---

## 项目用途

```text
生成邮箱/密码
  -> 打开注册页并填表
  -> 按压验证码
  ->（可选）绑定辅助邮箱
  -> 进入 Outlook 邮箱
  -> OAuth2（优先 cookie，失败再新浏览器并可注入 cookie）
  -> 将 refresh_token 追加写入 Results/oauth2.txt
```

OAuth 可处理：个人/工作帐户选择、保护帐户、验证电子邮件、保持登录「否」、同意授权与 code 捕获等。

---

## 主要功能

- 多代理并发注册  
- 按批成功上限重置程序内权重  
- Cookie 优先 OAuth + 冷启动/新环境兜底  
- 可选 temp_mail 辅助邮箱绑定  
- Ctrl+C：先汇总再关浏览器、清 profile  
- `log/` 进度与失败分类  

---

## 上游与致谢

- [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister) — 本项目二开来源  
- [Microsoft identity platform / Graph](https://learn.microsoft.com/en-us/graph/auth-v2-user) — OAuth2 与 Graph  
- [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) — 浏览器自动化  

---

## 开源协议

本项目采用 [MIT License](./LICENSE)。  
保留对上游 OutlookRegister 及所依赖 MIT 组件的署名说明。

---

## 友情链接

- <a href="https://linux.do"><img src="./linuxdo.webp" width="22" height="22" alt="LINUX DO" align="center"></a> [linux.do](https://linux.do)：**学AI，上L站！！！**
- [Nodeseek.com](https://www.nodeseek.com)：**Nodeseek是一个为热爱web开发、托管、vps /服务器和其他极客事物的人提供的地方。**
