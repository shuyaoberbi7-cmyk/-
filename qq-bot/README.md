# QQ 里的 Claude

用一个 QQ 小号挂 NapCat，`bot.py` 收到消息后交给 Claude 回复，再从小号发回来。

```
你的大号 ──私聊──> QQ 小号 (NapCat) ──WebSocket──> bot.py ──> Claude Code / Anthropic API
```

两种模式，改 `config.toml` 里的 `backend` 一行就能切换：

- `subscription`：调用你 Mac 上登录好的 Claude Code，用订阅额度，不用另外付费。只回 `owner_qq` 这个号的私聊，群消息一律不理。
- `api`：用 Anthropic API key，按量付费。可以在 `[api] groups` 里填群号，群里 @ 小号才会回。

## 在 Mac 上装起来

### 1. 装 Claude Code 并登录

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude        # 第一次运行会让你登录，选你的 Claude 订阅账号，登录完 /exit 退出
```

### 2. 用 Docker 跑 NapCat，登录小号

先装 [OrbStack](https://orbstack.dev)（比 Docker Desktop 轻），打开一次让它跑起来，然后：

```bash
docker run -d --name napcat --restart=always \
  -e NAPCAT_UID=$(id -u) -e NAPCAT_GID=$(id -g) \
  -p 3001:3001 -p 6099:6099 \
  mlikiowa/napcat-docker:latest

docker logs napcat    # 日志里有 WebUI 的 token，也会打印登录二维码
```

浏览器打开 http://127.0.0.1:6099/webui ，填日志里的 token，用小号手机 QQ 扫码登录。

登录后在 WebUI 的「网络配置」里新建一个 **WebSocket 服务器**：主机 `0.0.0.0`，端口 `3001`，消息格式选 `array`，token 自己随便设一个，然后启用。

### 3. 跑 bot

```bash
cd qq-bot
brew install python@3.12          # 需要 Python 3.11 以上
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.toml config.toml   # 填 owner_qq（大号）和上一步设的 token
cp style.example.md style.md         # 写你想让我在 QQ 里怎么说话
.venv/bin/python bot.py
```

看到「已连上 NapCat」就好了，用大号私聊小号试试。

`config.toml`、`style.md`、`state.json` 都在 `.gitignore` 里，QQ 号、key 和聊天记录不会被提交（这个仓库是公开的）。

## 日常用

- 发 `/new` 或 `新对话`：清掉这个会话的上下文，重新开始。
- 上下文存在 `state.json` 里，重启 bot 也接得上。
- 订阅模式下我拿不到任何工具：不能在你电脑上执行命令、读文件、联网，只能聊天。这是故意的。

### 别让 Mac 睡着

合盖或者睡眠后 bot 就断了。最简单的办法是这样启动：

```bash
caffeinate -i .venv/bin/python bot.py
```

或者在「系统设置 → 电池/能源」里打开「显示器关闭时防止自动睡眠」。

## 以后换成 API

1. 在 https://console.anthropic.com 建一个 API key，充值。
2. `config.toml` 里把 `backend` 改成 `"api"`，在 `[api]` 填 `api_key`，想开的群号填进 `groups`。
3. 重启 bot。

API 模式的上下文跟订阅模式的分开存，切换后会从头开始聊。

## 注意

- NapCat 不是 QQ 官方的协议，小号有被风控的可能，别用大号。
- 订阅模式的额度跟你在 claude.ai 上聊天是共用的，聊多了一样会碰到上限。
- 订阅额度只能你自己用，要拉群给别人用就切到 API 模式。
