# Codex Prompt: Bastion 跳板机管理系统

请按以下完整规格生成一个名为 `bastion` 的 Git 项目。这是一个基于 Tailscale 的轻量级跳板机管理系统。**请一次性生成所有文件的完整内容，不要省略或用 `...` 代替**。

---

## 一、项目背景与目标

用户想自建一个跳板机管理系统，解决以下需求：

1. **持久化 SSH 会话**：断网、关机后重连不丢失会话
2. **统一管理多台服务器**：通过 Web 面板查看所有可连接服务器
3. **一键连接**：在 Web 面板复制命令，粘贴到本地 SSH 客户端即可连接
4. **改善网络质量**：跳板机作为中继，改善直连较慢的线路
5. **安全**:面板仅在 Tailscale 内网暴露，跳板机入口由用户自行加固

---

## 二、架构原则

- 跳板机本身的 SSH 安全（YubiKey HOTP、Tailscale SSH 双入口）**由用户自行配置**，本项目不参与
- 目标服务器的连接通过 **Tailscale SSH**，无需 OTP（用户已配置好 Tailscale ACL）
- 服务器列表**自动从 `tailscale status --json` 读取**，无需手动维护
- 每台服务器对应一个独立的 **tmux session**（session 名 = 节点 hostname）
- 本地粘贴命令 → SSH 到跳板机 → 自动 attach 对应 tmux session → session 里已有（或新建）对 Tailscale 节点的 SSH 连接

---

## 三、工作流（最终体验）

```
用户浏览器打开面板(http://<tailscale_ip>:1234)
         ↓
看到所有 Tailscale 节点列表
每台服务器显示: hostname / IP / 在线状态 / 可编辑用户名 / 复制按钮
         ↓
修改用户名输入框,下方 SSH 命令实时变化
点击复制 → 本地 SSH 客户端粘贴
         ↓
SSH 到跳板机 → 自动 attach 同名 tmux session
         ↓
首次连接:session 内自动执行 tailscale ssh 到目标
后续连接:直接恢复之前的会话状态
         ↓
如 Tailscale 首次需要浏览器认证,URL 会显示在面板顶部
```

---

## 四、项目目录结构

```
bastion/
├── install.sh                      # 一键安装脚本
├── uninstall.sh                    # 卸载脚本
├── README.md                       # 使用文档(中文)
├── LICENSE                         # MIT
├── .gitignore
│
├── config/
│   ├── overrides.yaml.example      # 按 hostname 覆盖用户名/备注/标签
│   └── settings.yaml.example       # 端口、跳板机地址等
│
├── app/
│   ├── __init__.py
│   ├── main.py                     # Flask 主程序
│   ├── tailscale.py                # 封装 tailscale 命令
│   ├── tmux_manager.py             # 封装 tmux 查询
│   ├── config_loader.py            # 加载 YAML 配置
│   ├── auth_watcher.py             # 监控 Tailscale 认证 URL
│   ├── requirements.txt
│   └── templates/
│       └── index.html              # 前端页面(单文件)
│
├── scripts/
│   └── bastion-ssh                 # SSH 连接 wrapper(安装到 /usr/local/bin/)
│
└── deploy/
    ├── bastion.service             # systemd 服务
    └── nginx.conf                  # Nginx 反代配置
```

---

## 五、前端设计规范(重要)

### 设计哲学

**美观简单直接，功能全面**。不追求花哨动效，追求信息密度合理、操作路径清晰。

### 视觉风格

- **风格定位**：现代极简 + 终端工具感（类似 Linear、Vercel Dashboard、Tailscale Admin 的观感）
- **配色方案**：深色模式优先，同时支持浅色模式（通过 `@media (prefers-color-scheme)`）
  - 深色背景：`#0d1117`（主背景）、`#161b22`（卡片）、`#21262d`（边框）
  - 浅色背景：`#ffffff`（主背景）、`#f6f8fa`（卡片）、`#d0d7de`（边框）
  - 主色调：`#58a6ff`（链接/按钮）、`#3fb950`（在线状态绿）、`#f85149`（离线/警告红）、`#d29922`（待处理黄）
  - 文字：深色模式 `#e6edf3`（主）/ `#8b949e`（次），浅色模式 `#1f2328`（主）/ `#656d76`（次）
- **字体**：
  - UI 字体：`-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`
  - 代码字体：`"SF Mono", Monaco, "Cascadia Code", "JetBrains Mono", Consolas, monospace`
- **圆角**：统一使用 `6px`（卡片）和 `4px`（按钮、输入框）
- **间距**：8px 基准，关键间距 16px / 24px / 32px
- **无阴影、无渐变、无动画**（除了按钮 hover 过渡和复制成功反馈）
- **不使用 emoji 表情做功能图标**，用 SVG 图标或 Unicode 符号（●○✓⚠↻等）
- **状态圆点**：在线 `●`（绿色）、离线 `○`（灰色）、待认证 `●`（黄色）

### 布局结构

```
┌────────────────────────────────────────────────────────────┐
│  Bastion                                         [刷新]    │
│  跳板机管理面板 · 共 N 台服务器 · M 台在线                   │
├────────────────────────────────────────────────────────────┤
│  ⚠ 以下节点需要 Tailscale 认证:                             │
│  • server-c  [打开认证链接]  [已完成]                       │
├────────────────────────────────────────────────────────────┤
│  [全部 N]  [prod 3]  [dev 2]  [vps 1]                       │
│  搜索: [___________________]                                │
├────────────────────────────────────────────────────────────┤
│  ● web-prod                          prod  web      ♻      │
│  100.64.1.5 · 生产 Web 服务器                               │
│                                                            │
│  用户: [root___________]                                    │
│  ┌──────────────────────────────────────────────┐  ┌───┐   │
│  │ ssh -t root@jumpbox.ts.net "tmux new-s..."  │  │ 复制│  │
│  └──────────────────────────────────────────────┘  └───┘   │
├────────────────────────────────────────────────────────────┤
│  ○ old-backup                         backup               │
│  100.64.1.9 · 离线                                          │
│  ...                                                        │
├────────────────────────────────────────────────────────────┤
│  [下载 SSH config]                         最后刷新: 刚刚    │
└────────────────────────────────────────────────────────────┘
```

### 交互细节

1. **实时更新命令**：用户名输入框使用 `oninput` 事件,修改即实时更新下方 SSH 命令
2. **复制反馈**：点击复制按钮后,按钮文本变为 "已复制 ✓",背景变绿,2 秒后恢复
3. **自动刷新**：每 10 秒轮询 `/api/servers`,右下角显示 "最后刷新: X 秒前"
4. **标签筛选**：点击标签高亮,显示该标签下节点数。点第二次取消
5. **搜索**：按 hostname / note 实时过滤(前端完成,不请求后端)
6. **离线节点**：
   - 整个卡片 `opacity: 0.5`
   - 仍显示复制按钮(可预先准备命令)
   - 状态圆点为灰色空心 `○`
7. **tmux 已存在的节点**:卡片右上角显示 `♻ 活跃会话` 小标记(不是红点,是文字+图标)
8. **认证警告条**:
   - 顶部置顶显示,黄色背景淡化版(`rgba(210, 153, 34, 0.1)`)
   - "打开认证链接" 按钮跳转新标签
   - "已完成" 按钮调用 `/api/clear-auth` 后刷新
9. **键盘快捷键**:
   - `/` 聚焦搜索框
   - `Esc` 清空搜索
   - `r` 手动刷新
10. **响应式**:
    - 桌面:卡片宽度自适应,最小 400px,一行 1-2 个
    - 手机(<768px):单列,字体略小,按钮全宽
11. **空状态**:
    - 无节点:居中显示 "Tailscale 网络内暂无其他节点"
    - 搜索无结果:显示 "未找到匹配的服务器"
12. **无加载动画,无骨架屏**:数据返回即渲染,加载期间页面保持静态(避免闪烁)

### 代码组织要求

- 单文件 `index.html`,所有 CSS 内联在 `<style>`,所有 JS 内联在 `<script>`
- 不引入任何外部 CSS/JS 框架(不用 Tailwind、不用 jQuery、不用 Alpine)
- 纯原生 JavaScript ES6+,使用 `fetch` API
- CSS 使用 CSS 变量定义颜色,方便深浅色模式切换
- JavaScript 代码用 IIFE 包裹,避免全局污染
- 所有文本使用简体中文,代码/命令保持英文

---

## 六、技术栈

- **后端**：Python 3.11+ / Flask 3.x
- **WSGI**：gunicorn
- **前端**：原生 HTML + CSS + JavaScript,无框架
- **部署**：systemd + Nginx
- **配置**：YAML (PyYAML)
- **目标系统**：Debian 12 / Ubuntu 22.04+

---

## 七、各文件详细规格

### 7.1 `install.sh`

必须幂等,可重复运行。

**职责**:

1. 检查运行环境
   - 必须以 root 运行
   - 系统必须是 Debian/Ubuntu
   - `tailscale` 命令必须已存在(否则退出并提示先安装 Tailscale)
2. 安装系统依赖:`tmux nginx python3 python3-pip python3-venv`
3. 项目安装目录:`/opt/bastion`
   - 复制 `app/` 到 `/opt/bastion/app/`
   - 创建 venv:`/opt/bastion/venv/`
   - 安装 `requirements.txt`
4. 配置目录 `/etc/bastion/`
   - 复制 `config/*.yaml.example` 为 `/etc/bastion/*.yaml`(已存在则跳过)
5. 安装 wrapper 脚本:`scripts/bastion-ssh` → `/usr/local/bin/bastion-ssh` (+x)
6. 安装 systemd 服务:`deploy/bastion.service` → `/etc/systemd/system/bastion.service`
7. 安装 Nginx 配置:
   - 自动读取 `tailscale ip -4` 获取 Tailscale IP
   - 替换 `deploy/nginx.conf` 中的 `{{TAILSCALE_IP}}` 占位符
   - 写入 `/etc/nginx/sites-available/bastion`
   - 软链到 `/etc/nginx/sites-enabled/bastion`
8. `systemctl daemon-reload && systemctl enable --now bastion`
9. `nginx -t && systemctl reload nginx`
10. 打印彩色总结:安装路径、配置路径、访问地址、日志查看命令

**错误处理**:
- 使用 `set -euo pipefail`
- 每个关键步骤包装成函数
- 失败时彩色输出错误行号和提示
- 彩色输出函数:`log_info`(绿) / `log_warn`(黄) / `log_error`(红)

### 7.2 `uninstall.sh`

**职责**:
- 停止并禁用 systemd 服务
- 删除 `/etc/systemd/system/bastion.service`
- 删除 Nginx 配置
- 删除 `/opt/bastion/` 和 `/usr/local/bin/bastion-ssh`
- **询问用户**是否保留 `/etc/bastion/` 配置(默认保留)
- **不清理** tmux sessions(用户的会话数据)

### 7.3 `config/settings.yaml.example`

```yaml
# Bastion 全局配置

web:
  # Web 面板端口
  port: 1234
  # 绑定地址: auto=自动检测 Tailscale IP, 或手动填 IP
  bind_address: auto

jumpbox:
  # 生成复制命令时使用的跳板机地址
  # auto=使用 Tailscale MagicDNS hostname, 或自定义如 jumpbox.example.com
  host: auto
  # SSH 到跳板机时使用的用户名
  ssh_user: root

defaults:
  # 连接目标服务器时的默认用户名
  target_user: root
  # tmux session 命名前缀(留空即 hostname 本身作为 session 名)
  tmux_prefix: ""

ui:
  # 面板标题
  title: "Bastion"
  # 副标题
  subtitle: "跳板机管理面板"
  # 自动刷新间隔(秒, 0=禁用)
  refresh_interval: 10
```

### 7.4 `config/overrides.yaml.example`

```yaml
# 按 Tailscale hostname 覆盖默认配置
# hostname 即 `tailscale status` 中显示的机器名

overrides:
  # 示例配置,去掉注释使用:
  #
  # web-prod-01:
  #   user: ubuntu               # 覆盖默认 target_user
  #   note: "生产 Web 服务器"     # 面板显示的备注
  #   tags: [production, web]    # 自定义标签(会与 Tailscale tags 合并去重)
  #   hidden: false              # 是否在面板隐藏(默认 false)
  #
  # dev-db:
  #   user: postgres
  #   note: "开发数据库"
  #   tags: [dev, database]
  #
  # internal-test:
  #   hidden: true               # 完全在面板隐藏
```

### 7.5 `app/requirements.txt`

```
Flask==3.0.3
PyYAML==6.0.2
gunicorn==23.0.0
```

### 7.6 `app/tailscale.py`

```python
"""封装 tailscale 命令调用"""

import json
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _run_tailscale(args: list[str]) -> Optional[str]:
    """运行 tailscale 命令,失败返回 None"""
    try:
        result = subprocess.run(
            ["tailscale"] + args,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.error(f"tailscale {' '.join(args)} failed: {e}")
        return None


def get_status() -> Optional[dict]:
    """调用 `tailscale status --json`,返回解析后的 JSON"""
    output = _run_tailscale(["status", "--json"])
    if output is None:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse tailscale status JSON: {e}")
        return None


def get_nodes() -> list[dict]:
    """
    返回所有 Tailscale 节点列表(不含自己),每个节点包含:
    - hostname: str
    - ip: str (Tailscale IPv4, 100.x.x.x)
    - online: bool
    - os: str (linux/darwin/windows)
    - tags: list[str] (Tailscale ACL tags, 去掉 'tag:' 前缀)
    """
    status = get_status()
    if status is None:
        return []

    nodes = []
    peer_map = status.get("Peer", {}) or {}
    for peer in peer_map.values():
        # 提取 IPv4 地址
        ipv4 = None
        for ip in peer.get("TailscaleIPs", []) or []:
            if ":" not in ip:
                ipv4 = ip
                break
        if not ipv4:
            continue

        # 处理 tags (去掉 tag: 前缀)
        raw_tags = peer.get("Tags", []) or []
        tags = [t.removeprefix("tag:") for t in raw_tags]

        nodes.append({
            "hostname": peer.get("HostName", "unknown"),
            "ip": ipv4,
            "online": peer.get("Online", False),
            "os": peer.get("OS", "unknown"),
            "tags": tags,
        })

    return nodes


def get_self_hostname() -> Optional[str]:
    """返回本机(跳板机)的 Tailscale hostname(MagicDNS 格式)"""
    status = get_status()
    if status is None:
        return None
    self_node = status.get("Self", {})
    dns = self_node.get("DNSName", "")
    # DNSName 形如 "jumpbox.tailxxxx.ts.net."
    return dns.rstrip(".") if dns else self_node.get("HostName")


def get_self_ip() -> Optional[str]:
    """返回本机的 Tailscale IPv4"""
    output = _run_tailscale(["ip", "-4"])
    if output is None:
        return None
    return output.strip().splitlines()[0] if output.strip() else None
```

### 7.7 `app/tmux_manager.py`

```python
"""封装 tmux session 查询"""

import subprocess
import logging

logger = logging.getLogger(__name__)


def list_sessions() -> list[dict]:
    """
    返回当前所有 tmux sessions
    每个 session: {name: str, created_at: int, attached: bool}
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F",
             "#{session_name}|#{session_created}|#{?session_attached,1,0}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            # tmux 未启动或无 session
            return []
        sessions = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) == 3:
                sessions.append({
                    "name": parts[0],
                    "created_at": int(parts[1]),
                    "attached": parts[2] == "1",
                })
        return sessions
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.error(f"tmux list-sessions failed: {e}")
        return []


def session_exists(name: str) -> bool:
    """检查指定 session 是否存在"""
    return any(s["name"] == name for s in list_sessions())
```

### 7.8 `app/auth_watcher.py`

```python
"""监控 Tailscale 认证 URL"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_DIR = Path("/tmp/bastion-auth")


def get_pending_auth_urls() -> dict[str, str]:
    """
    返回 {hostname: url} 字典,列出所有待认证的 URL
    扫描 /tmp/bastion-auth/*.url 文件
    """
    if not AUTH_DIR.exists():
        return {}

    result = {}
    try:
        for file in AUTH_DIR.glob("*.url"):
            hostname = file.stem
            try:
                url = file.read_text().strip()
                if url.startswith("https://login.tailscale.com/"):
                    result[hostname] = url
            except Exception as e:
                logger.warning(f"Failed to read auth file {file}: {e}")
    except Exception as e:
        logger.error(f"Failed to scan auth directory: {e}")

    return result


def clear_auth_url(hostname: str) -> bool:
    """手动清除某个 hostname 的认证 URL 记录"""
    # 安全校验: hostname 不能包含路径分隔符
    if "/" in hostname or ".." in hostname:
        return False

    file = AUTH_DIR / f"{hostname}.url"
    try:
        if file.exists():
            file.unlink()
        return True
    except Exception as e:
        logger.error(f"Failed to clear auth URL for {hostname}: {e}")
        return False
```

### 7.9 `app/config_loader.py`

```python
"""加载 YAML 配置"""

import yaml
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path("/etc/bastion")

DEFAULT_SETTINGS = {
    "web": {"port": 1234, "bind_address": "auto"},
    "jumpbox": {"host": "auto", "ssh_user": "root"},
    "defaults": {"target_user": "root", "tmux_prefix": ""},
    "ui": {
        "title": "Bastion",
        "subtitle": "跳板机管理面板",
        "refresh_interval": 10,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_settings() -> dict:
    """加载全局配置,与默认值合并"""
    path = CONFIG_DIR / "settings.yaml"
    if not path.exists():
        logger.warning(f"{path} not found, using defaults")
        return DEFAULT_SETTINGS.copy()

    try:
        with open(path, "r", encoding="utf-8") as f:
            user_settings = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULT_SETTINGS, user_settings)
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        return DEFAULT_SETTINGS.copy()


def load_overrides() -> dict:
    """加载 overrides 字典"""
    path = CONFIG_DIR / "overrides.yaml"
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("overrides", {}) or {}
    except Exception as e:
        logger.error(f"Failed to load overrides: {e}")
        return {}


def merge_node_info(node: dict, overrides: dict, settings: dict) -> dict:
    """
    合并节点信息:
    输入: 从 tailscale 拿到的 node + overrides 字典 + 全局 settings
    输出: 完整的节点信息 dict
    """
    hostname = node["hostname"]
    override = overrides.get(hostname, {}) or {}
    defaults = settings.get("defaults", {})

    merged_tags = list(dict.fromkeys(
        (node.get("tags") or []) + (override.get("tags") or [])
    ))

    return {
        "hostname": hostname,
        "ip": node["ip"],
        "online": node["online"],
        "os": node.get("os", "unknown"),
        "user": override.get("user") or defaults.get("target_user", "root"),
        "note": override.get("note", ""),
        "tags": merged_tags,
        "hidden": bool(override.get("hidden", False)),
    }
```

### 7.10 `app/main.py`

```python
"""Flask 主程序"""

import logging
from flask import Flask, render_template, jsonify, request, Response

from tailscale import get_nodes, get_self_hostname
from tmux_manager import list_sessions, session_exists
from auth_watcher import get_pending_auth_urls, clear_auth_url
from config_loader import load_settings, load_overrides, merge_node_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)


def _resolve_jumpbox_host(settings: dict) -> str:
    """解析跳板机显示地址"""
    host = settings["jumpbox"]["host"]
    if host == "auto":
        detected = get_self_hostname()
        return detected or "jumpbox"
    return host


@app.route("/")
def index():
    settings = load_settings()
    return render_template(
        "index.html",
        title=settings["ui"]["title"],
        subtitle=settings["ui"]["subtitle"],
        refresh_interval=settings["ui"]["refresh_interval"],
    )


@app.route("/api/servers")
def api_servers():
    settings = load_settings()
    overrides = load_overrides()

    raw_nodes = get_nodes()
    sessions = {s["name"] for s in list_sessions()}
    auth_urls = get_pending_auth_urls()

    servers = []
    for node in raw_nodes:
        info = merge_node_info(node, overrides, settings)
        if info["hidden"]:
            continue
        session_name = settings["defaults"]["tmux_prefix"] + info["hostname"]
        info["tmux_session_exists"] = session_name in sessions
        info["pending_auth_url"] = auth_urls.get(info["hostname"])
        servers.append(info)

    # 排序: 在线优先,再按 hostname
    servers.sort(key=lambda s: (not s["online"], s["hostname"]))

    return jsonify({
        "jumpbox_host": _resolve_jumpbox_host(settings),
        "jumpbox_user": settings["jumpbox"]["ssh_user"],
        "tmux_prefix": settings["defaults"]["tmux_prefix"],
        "default_target_user": settings["defaults"]["target_user"],
        "servers": servers,
    })


@app.route("/api/ssh-config")
def api_ssh_config():
    """返回本地 ~/.ssh/config 可用的配置片段"""
    settings = load_settings()
    overrides = load_overrides()
    jumpbox_host = _resolve_jumpbox_host(settings)
    jumpbox_user = settings["jumpbox"]["ssh_user"]
    tmux_prefix = settings["defaults"]["tmux_prefix"]

    lines = [
        "# Bastion generated SSH config",
        "",
        "Host jumpbox",
        f"  HostName {jumpbox_host}",
        f"  User {jumpbox_user}",
        "",
    ]

    for node in get_nodes():
        info = merge_node_info(node, overrides, settings)
        if info["hidden"]:
            continue
        alias = info["hostname"]
        tmux_name = tmux_prefix + alias
        lines.extend([
            f"Host {alias}",
            f"  HostName {jumpbox_host}",
            f"  User {jumpbox_user}",
            f'  RemoteCommand tmux new-session -A -s {tmux_name} bastion-ssh {alias} {info["user"]}',
            f"  RequestTTY yes",
            "",
        ])

    text = "\n".join(lines)
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=bastion-ssh-config"},
    )


@app.route("/api/clear-auth", methods=["POST"])
def api_clear_auth():
    data = request.get_json(silent=True) or {}
    hostname = data.get("hostname", "")
    if not hostname:
        return jsonify({"ok": False, "error": "hostname required"}), 400
    ok = clear_auth_url(hostname)
    return jsonify({"ok": ok})


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
```

### 7.11 `scripts/bastion-ssh`

```bash
#!/bin/bash
# bastion-ssh: 封装 tailscale ssh,捕获 Tailscale 认证 URL
# 用法: bastion-ssh <hostname> <user>
#
# 功能:
#   1. 运行 `tailscale ssh user@hostname`,保持交互式
#   2. 捕获输出中的 login.tailscale.com URL,写入
#      /tmp/bastion-auth/<hostname>.url 供面板轮询
#   3. 检测到连接建立后删除该文件
#   4. 退出时清理

set -uo pipefail

HOSTNAME="${1:-}"
USER="${2:-root}"

if [ -z "$HOSTNAME" ]; then
    echo "Usage: bastion-ssh <hostname> <user>" >&2
    exit 2
fi

AUTH_DIR="/tmp/bastion-auth"
AUTH_FILE="$AUTH_DIR/${HOSTNAME}.url"

mkdir -p "$AUTH_DIR"

cleanup() {
    rm -f "$AUTH_FILE" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 用命名管道 + tee 同时保持 TTY 和捕获输出
# 通过 script 命令保留 PTY 交互性
SCRIPT_LOG=$(mktemp /tmp/bastion-ssh-log.XXXXXX)
trap 'rm -f "$SCRIPT_LOG"; cleanup' EXIT INT TERM

# 后台监控日志文件,提取认证 URL
(
    while [ -f "$SCRIPT_LOG" ]; do
        if [ -s "$SCRIPT_LOG" ]; then
            URL=$(grep -oE 'https://login\.tailscale\.com/a/[a-zA-Z0-9]+' "$SCRIPT_LOG" 2>/dev/null | head -n1)
            if [ -n "$URL" ] && [ ! -f "$AUTH_FILE" ]; then
                echo "$URL" > "$AUTH_FILE"
            fi
        fi
        sleep 1
    done
) &
MONITOR_PID=$!

# 运行 tailscale ssh,用 script 保留 PTY
# -q 静默 script 自身的启动/退出提示
# -f 每行写入后 flush
script -q -f -c "tailscale ssh ${USER}@${HOSTNAME}" "$SCRIPT_LOG"
RC=$?

kill "$MONITOR_PID" 2>/dev/null || true
wait "$MONITOR_PID" 2>/dev/null || true
cleanup
rm -f "$SCRIPT_LOG"

exit "$RC"
```

### 7.12 `app/templates/index.html`

必须是**完整的单文件 HTML**,包含:

1. 完整的 `<head>`(meta viewport、title、CSS变量定义)
2. 完整的 `<style>`(深浅色模式、所有卡片/按钮/输入框样式、响应式)
3. 完整的 `<body>`(静态骨架 + JavaScript 填充数据)
4. 完整的 `<script>`(IIFE 包裹,包含:
   - 数据获取(`fetch /api/servers`)
   - 渲染函数(服务器卡片、认证警告、标签筛选)
   - 用户名输入实时更新命令
   - 复制按钮逻辑
   - 搜索过滤
   - 键盘快捷键(`/` 聚焦搜索、`Esc` 清空、`r` 刷新)
   - 自动轮询
   - "最后刷新" 时间显示)

**严格遵循第五章前端设计规范**,生成完整的代码。不要使用任何前端框架。

### 7.13 `deploy/bastion.service`

```ini
[Unit]
Description=Bastion - Jumpbox Management Panel
Documentation=https://github.com/YOUR_GITHUB/bastion
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/bastion/app
Environment="PATH=/opt/bastion/venv/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/bastion/venv/bin/gunicorn \
    --workers 2 \
    --bind 127.0.0.1:8000 \
    --access-logfile - \
    --error-logfile - \
    main:app
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 7.14 `deploy/nginx.conf`

```nginx
# Bastion Nginx 配置
# {{TAILSCALE_IP}} 会被 install.sh 替换为实际 Tailscale IP

server {
    listen {{TAILSCALE_IP}}:1234;
    server_name _;

    access_log /var/log/nginx/bastion-access.log;
    error_log /var/log/nginx/bastion-error.log;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;
    }
}
```

### 7.15 `README.md`

完整的中文文档,至少包含以下章节(每节有实质内容,不是空壳):

1. **Bastion - 基于 Tailscale 的跳板机管理系统**(一句话介绍)
2. **特性**(要点列表:持久会话、自动发现、一键连接、Tailscale 集成、轻量)
3. **架构**(ASCII 流程图说明工作原理)
4. **前置要求**(Debian 12+ 或 Ubuntu 22.04+、已安装 Tailscale 并登录、Tailscale SSH 已启用、跳板机 SSH 自行加固)
5. **快速安装**(git clone、sudo ./install.sh、访问地址)
6. **首次使用**(如何复制命令、本地配置 jumpbox 别名、`~/.ssh/config` 示例)
7. **配置说明**
   - `/etc/bastion/settings.yaml` 每个字段解释
   - `/etc/bastion/overrides.yaml` 使用方法
8. **tmux 使用技巧**(如何列出、切换、删除 session 的命令)
9. **常见问题 FAQ**
   - Tailscale 首次认证怎么处理
   - 如何删除不需要的 tmux session
   - 修改配置后如何生效
   - 跳板机的 SSH 怎么加固
10. **安全说明**(明确声明:本系统不负责跳板机入口安全,用户需自行配置 SSH 密钥/YubiKey/Tailscale SSH 等)
11. **卸载**
12. **License**

### 7.16 `.gitignore`

```
# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
venv/
.venv/

# 本地配置(只提交 .example)
config/*.yaml
!config/*.yaml.example

# 日志
*.log

# 编辑器
.vscode/
.idea/
*.swp
.DS_Store

# 测试
.pytest_cache/
.coverage
```

### 7.17 `LICENSE`

标准 MIT License,Copyright year 2025,holder 留空 `YOUR_NAME`。

---

## 八、代码质量要求

1. **Python**:
   - 遵循 PEP 8
   - 使用 type hints
   - 每个公共函数都有 docstring
   - 所有外部调用(subprocess、文件 IO、YAML 解析)都要 try/except
   - 使用 `logging` 模块,不用 `print`

2. **JavaScript**:
   - ES6+ 语法
   - IIFE 包裹,避免全局污染
   - 使用 `const`/`let`,不用 `var`
   - `fetch` + async/await

3. **Shell**:
   - `set -euo pipefail`
   - 所有变量加双引号
   - 函数化组织,便于阅读

4. **HTML/CSS**:
   - 语义化标签
   - CSS 变量定义主题色
   - 响应式设计(移动端单列)

5. **幂等性**:install.sh 必须可重复运行而不破坏现有配置

6. **注释**:关键逻辑写清楚,但不要过度注释显而易见的代码

---

## 九、验收标准

完成后必须满足:

1. 在 Debian 12 上 `git clone && sudo ./install.sh` 能一次性跑通
2. 安装完成后访问 `http://<tailscale_ip>:1234` 能看到面板
3. 面板正确列出 Tailscale 网络所有其他节点
4. 修改用户名输入框,下方 SSH 命令实时更新
5. 点击复制,粘贴到本地 SSH 能连上跳板机并进入 tmux
6. 首次连接目标节点时如有 Tailscale 认证 URL,面板顶部能显示
7. 断开本地 SSH 重连,tmux session 内容保留
8. 编辑 `/etc/bastion/overrides.yaml` 刷新面板能看到变化
9. 前端在深色模式和浅色模式下都美观可用
10. 手机浏览器访问也能正常使用(响应式)

---

## 十、输出格式

请按以下格式输出所有文件:

```
### install.sh
```bash
[完整内容]
```

### uninstall.sh
```bash
[完整内容]
```

### README.md
```markdown
[完整内容]
```

...(以此类推每个文件)
```

**不要省略任何文件内容,不要用 `...` 代替,不要跳过任何细节**。前端 HTML 文件预计在 600-1000 行,请完整生成。
