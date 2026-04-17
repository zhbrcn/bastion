# Bastion - 基于 Tailscale 的跳板机管理系统

Bastion 是一个基于 Tailscale 的轻量级跳板机管理面板，用来集中查看节点、生成一键连接命令，并通过 `tmux` 保持跨断线的持久 SSH 会话。

## 特性

- 持久会话：每台目标服务器对应一个独立 `tmux session`，断线或重连后继续之前的工作状态。
- 自动发现：直接读取 `tailscale status --json`，无需手工维护服务器列表。
- 一键连接：Web 面板实时生成 SSH 命令，复制后粘贴到本地终端即可使用。
- Tailscale 集成：目标节点通过 `tailscale ssh` 接入，首次认证链接会被面板捕获并展示。
- 轻量部署：后端使用 Flask，运行在 Gunicorn 后，前面由 Nginx 反代。
- 配置清晰：通过 YAML 维护全局设置和单节点覆盖项。
- 响应式界面：桌面和手机浏览器都能正常使用。

## 架构

```text
浏览器打开 http://<tailscale_ip>:1234
        |
        v
Flask 面板读取 tailscale status --json
        |
        +--> 展示 hostname / IP / 在线状态 / 备注 / 标签
        +--> 查询 tmux session 状态
        +--> 展示待处理的 Tailscale 认证 URL
        |
        v
用户复制 SSH 命令
        |
        v
本地 SSH -> 跳板机 -> tmux new-session -A -s <hostname> bastion-ssh <hostname> <user>
        |
        v
bastion-ssh 调用 tailscale ssh <user>@<hostname>
        |
        +--> 首次认证时写入 /tmp/bastion-auth/<hostname>.url
        +--> 已有会话时直接恢复 tmux 中断前状态
```

## 前置要求

- Debian 12+ 或 Ubuntu 22.04+
- 已安装并登录 Tailscale
- 已启用并配置好 Tailscale SSH
- 跳板机自身 SSH 安全加固由你自行负责
- 目标服务器已经允许从跳板机通过 Tailscale SSH 连接

本项目只负责管理面板、会话恢复和连接包装，不负责替代跳板机入口的安全策略。

## 快速安装

```bash
git clone <your-repo-url> bastion
cd bastion
sudo ./install.sh
```

安装完成后，脚本会输出：

- 安装目录：`/opt/bastion`
- 配置目录：`/etc/bastion`
- 访问地址：`http://<tailscale_ip>:1234`
- 查看日志：`journalctl -u bastion -f`

## 首次使用

打开浏览器访问：

```text
http://<tailscale_ip>:1234
```

面板会显示当前 Tailnet 中的其他节点。每张卡片包含：

- 节点 hostname
- Tailscale IPv4
- 在线/离线状态
- 可编辑的目标用户名
- 实时更新的 SSH 命令
- 复制按钮

典型流程：

1. 在卡片中确认或修改目标用户名。
2. 点击“复制”。
3. 在本地终端粘贴执行。
4. 首次连接时如果 Tailscale 需要浏览器认证，面板顶部会出现认证链接。
5. 认证完成后，再次连接即可恢复或进入对应 `tmux session`。

如果你希望本地更简洁地跳到跳板机，可以在自己的 `~/.ssh/config` 中加入：

```sshconfig
Host jumpbox
  HostName jumpbox.example.ts.net
  User root
```

面板还提供 `/api/ssh-config` 下载，可直接生成适合写入 `~/.ssh/config` 的片段。

## 配置说明

### `/etc/bastion/settings.yaml`

示例：

```yaml
web:
  port: 1234
  bind_address: auto

jumpbox:
  host: auto
  ssh_user: root

defaults:
  target_user: root
  tmux_prefix: ""

ui:
  title: "Bastion"
  subtitle: "跳板机管理面板"
  refresh_interval: 10
```

字段说明：

- `web.port`：面板端口，默认 `1234`。
- `web.bind_address`：面板绑定地址。`auto` 表示自动探测当前机器的 Tailscale IPv4。
- `jumpbox.host`：前端生成命令时显示的跳板机地址。`auto` 表示优先使用 Tailscale MagicDNS hostname。
- `jumpbox.ssh_user`：连接跳板机时使用的 SSH 用户。
- `defaults.target_user`：连接目标节点时默认使用的用户名。
- `defaults.tmux_prefix`：每个 `tmux session` 的前缀，默认空字符串。
- `ui.title`：页面标题。
- `ui.subtitle`：页面副标题。
- `ui.refresh_interval`：前端自动刷新间隔，单位秒，`0` 表示禁用自动刷新。

修改 `settings.yaml` 后执行：

```bash
sudo systemctl restart bastion
```

### `/etc/bastion/overrides.yaml`

示例：

```yaml
overrides:
  web-prod-01:
    user: ubuntu
    note: "生产 Web 服务器"
    tags: [production, web]
    hidden: false

  dev-db:
    user: postgres
    note: "开发数据库"
    tags: [dev, database]

  internal-test:
    hidden: true
```

字段说明：

- `user`：覆盖该节点的默认登录用户。
- `note`：在面板中显示的备注。
- `tags`：自定义标签，会和 Tailscale 自身标签合并去重。
- `hidden`：是否在面板中隐藏该节点。

修改 `overrides.yaml` 后无需重启服务，刷新浏览器即可看到变化。

## tmux 使用技巧

每台目标节点的会话名默认等于 hostname，或等于 `tmux_prefix + hostname`。

常用命令：

```bash
tmux list-sessions
tmux attach -t <session_name>
tmux switch-client -t <session_name>
tmux kill-session -t <session_name>
```

建议做法：

- 长任务直接在对应节点会话中运行。
- 断开本地 SSH 不会结束远端 `tmux session`。
- 不再需要某个节点历史会话时，再手动执行 `tmux kill-session`。

## 常见问题 FAQ

### Tailscale 首次认证怎么处理？

这是 `tailscale ssh` 的正常行为。`scripts/bastion-ssh` 会捕获输出中的认证 URL，并写入 `/tmp/bastion-auth/<hostname>.url`。面板检测到后会在顶部显示“打开认证链接”按钮。完成认证后点击“已完成”，或重新连接即可。

### 如何删除不再需要的 tmux session？

直接在跳板机上执行：

```bash
tmux list-sessions
tmux kill-session -t <session_name>
```

卸载脚本不会自动清理 `tmux session`，避免误删你的会话数据。

### 修改配置后如何生效？

- 修改 `/etc/bastion/settings.yaml`：执行 `sudo systemctl restart bastion`
- 修改 `/etc/bastion/overrides.yaml`：刷新页面即可
- 修改 Nginx 配置：执行 `sudo nginx -t && sudo systemctl reload nginx`

### 跳板机的 SSH 怎么加固？

本项目不负责跳板机入口安全。你应自行配置：

- SSH 公钥认证
- 禁止密码登录
- YubiKey / FIDO2 / OTP
- Tailscale SSH
- 防火墙与最小暴露面

## 安全说明

这个项目的边界非常明确：

- 它只负责管理面板、tmux 会话恢复、Tailscale SSH 包装。
- 它不负责跳板机自身 SSH 入口的安全策略。
- 它不替你配置密钥、OTP、YubiKey、Fail2ban 或堡垒机审计。
- 它默认建议只通过 Tailscale 内网暴露面板。

换句话说，跳板机入口如何做强认证、最小权限和审计，必须由你自己负责。

## 卸载

执行：

```bash
sudo ./uninstall.sh
```

卸载行为：

- 停止并禁用 `bastion.service`
- 删除 systemd 文件
- 删除 Nginx 配置
- 删除 `/opt/bastion`
- 删除 `/usr/local/bin/bastion-ssh`
- 询问是否保留 `/etc/bastion`
- 不清理已有 `tmux session`

## License

本项目使用 MIT License，详见仓库中的 [LICENSE](LICENSE)。
