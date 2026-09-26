# OutlookGetToken

给**已注册**但还没有 Graph `refresh_token` 的 Outlook / Hotmail 账号，批量走一遍 OAuth2 授权，把 token 写入结果文件。

核心脚本：`get_refresh_token.py`（patchright 浏览器自动化 + 授权码换 token）。

## 相关项目

- **[OutlookRegister](https://github.com/daimon3332/OutlookRegister) — 账号注册。** 自动注册 Outlook / Hotmail 账号，并获取 Microsoft Graph OAuth2 `refresh_token`。
- **[OutlookManage](https://github.com/daimon3332/OutlookManage) — 账号管理。** 用于导入、检测、刷新和管理已注册的 Outlook 账号及其 OAuth2 凭据。

---

## 1. 如何使用

### 1.1 环境

```bash
git clone https://github.com/daimon3332/Outlook-Oauth-GetToken.git
cd Outlook-Oauth-GetToken
pip install patchright requests
patchright install chromium
```

代理软件需已启动（默认示例为本机 `127.0.0.1:7890`）。

### 1.2 准备配置与账号

首次使用：

```bash
# Windows
copy config.example.json config.json
copy not_oauth2.example.txt not_oauth2.txt

# Linux / macOS
cp config.example.json config.json
cp not_oauth2.example.txt not_oauth2.txt
```

编辑 `not_oauth2.txt`，**每行一个账号**：

```text
邮箱----密码
```

示例：

```text
user1@outlook.com----YourPass1
user2@hotmail.com----YourPass2
```

- 以 `#` 开头的行视为注释，忽略  
- 也兼容更长行（如已有 `邮箱----密码----client_id----token`）：**只取前两段** 邮箱与密码  

按需编辑 `config.json`（代理、并发等，见下一节）。

### 1.3 运行

```bash
python get_refresh_token.py
```

成功后会**追加**写入 `oauth2.txt`：

```text
邮箱----密码----client_id----refresh_token
```

### 1.4 使用注意

- 工具会弹真实浏览器窗口（`headless=False`）  
- **不会**自动绑定/接码辅助邮箱；密码后的「保护帐户」页会点 **暂时跳过**  
- 若已绑过辅助邮箱且弹出「验证你的电子邮件」，会点 **使用密码** 继续  
- 出现 **帐户已锁定** → 该号直接失败  
- 同一账号多次成功会重复追加到输出文件，请自行去重  

---

## 2. `config.json` 配置

将 `config.example.json` 复制为 `config.json` 后修改：

```json
{
    "input_file": "not_oauth2.txt",
    "output_file": "oauth2.txt",
    "concurrent": 2,
    "proxy": {
        "mode": "single",
        "type": "http",
        "host": "127.0.0.1",
        "single_port": 7890,
        "port_start": 24000,
        "port_end": 24005,
        "max_per_proxy": 20
    }
}
```

### 2.1 顶层字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `input_file` | string | 输入文件名，默认 `not_oauth2.txt` |
| `output_file` | string | 输出文件名，默认 `oauth2.txt`，**追加**写入 |
| `concurrent` | number | 并发浏览器数；实际为 `min(concurrent, 账号数)` |
| `proxy` | object | 代理配置，见下表 |

### 2.2 `proxy`

| 字段 | 说明 |
|------|------|
| `mode` | `single`：只用 `single_port`；`multiple`：使用 `port_start`～`port_end` 端口池 |
| `type` | 代理协议，如 `http`、`socks5` |
| `host` | 代理主机，常见 `127.0.0.1` |
| `single_port` | `mode=single` 时的端口 |
| `port_start` / `port_end` | `mode=multiple` 时的端口闭区间 |
| `max_per_proxy` | 单个端口在进程内最多被选中次数；用满后该端口暂不选，全满则重置计数再选 |

---

## 3. 代码流程

入口：`python get_refresh_token.py` → `main()`。

### 3.1 总览

```text
读 config.json 与账号文件
  → 按 concurrent 并发处理每个账号
  → 成功：追加写入 oauth2.txt
  → 失败：记录原因后继续下一个
```

### 3.2 单账号流程

```text
启动 Chromium（带代理）
  → 打开 Graph OAuth 授权页
  → 状态循环（约 120s 内）
       ├─ 一旦出现 localhost...?code=     → 立刻结束登录，换 token
       ├─ 「帐户已锁定」                   → 失败
       ├─ 「验证你的电子邮件」              → 点「使用密码」
       ├─ 密码框                           → 填密码 + 「下一步」
       ├─ 邮箱框                           → 填登录邮箱
       ├─ 「让我们来保护你的帐户」           → 「暂时跳过」
       ├─ 「保持登录状态？」                 → 点「否」
       └─ 授权同意页                       → 接受
  → 用 code 换 refresh_token
  → 关浏览器
```

### 3.3 密码之后的分支

**保护帐户 / 锁定页都在输入密码之后。**

| 页面 | 行为 |
|------|------|
| 验证你的电子邮件 | 点「使用密码」 |
| 输入密码 | 填写并点「下一步」 |
| 让我们来保护你的帐户 | 点「暂时跳过」 |
| 帐户已锁定 | 直接失败 |
| 保持登录 | 点「否」 |
| 同意 / 已授权 | 点接受，或直接拿到 code |

---

## 友情链接

- <a href="https://linux.do"><img src="./linuxdo.webp" width="22" height="22" alt="LINUX DO" align="center"></a> [linux.do](https://linux.do)：**学AI，上L站！！！**
- [Nodeseek.com](https://www.nodeseek.com)：**Nodeseek是一个为热爱web开发、托管、vps /服务器和其他极客事物的人提供的地方。**
