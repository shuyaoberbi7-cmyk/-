"""QQ <-> Claude 桥接：NapCat (OneBot v11 WebSocket) 收消息，交给 Claude 回复。"""

import asyncio
import json
import logging
import os
import sys
import tomllib
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
WORKDIR = HERE / "workdir"
MAX_SEGMENT = 1500  # QQ 单条消息太长会发不出去，超过就拆开发
RESET_COMMANDS = {"/new", "/reset", "新对话"}

log = logging.getLogger("qq-bot")


def load_config() -> dict:
    path = HERE / "config.toml"
    if not path.exists():
        sys.exit("没找到 config.toml，先 cp config.example.toml config.toml 再改")
    with path.open("rb") as f:
        return tomllib.load(f)


def load_style() -> str:
    for name in ("style.md", "style.example.md"):
        path = HERE / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


class State:
    """按会话保存上下文：订阅模式存 Claude Code 的 session_id，API 模式存消息历史。"""

    def __init__(self):
        self.data = {"sessions": {}, "histories": {}}
        if STATE_FILE.exists():
            self.data.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))

    def save(self):
        STATE_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def reset(self, key: str):
        self.data["sessions"].pop(key, None)
        self.data["histories"].pop(key, None)
        self.save()


class SubscriptionBackend:
    """调用本机登录好的 Claude Code（claude -p），用的是订阅额度。"""

    def __init__(self, cfg: dict, style: str, state: State):
        self.bin = cfg.get("claude_bin", "claude")
        self.model = cfg.get("model", "")
        self.timeout = cfg.get("timeout_seconds", 180)
        self.mcp_config = cfg.get("mcp_config", "")
        self.mcp_servers = []
        if self.mcp_config:
            path = (HERE / self.mcp_config).resolve()
            self.mcp_config = str(path)
            self.mcp_servers = list(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))
        self.style = style
        self.state = state
        WORKDIR.mkdir(exist_ok=True)

    async def reply(self, key: str, text: str) -> str:
        args = [
            self.bin, "-p", text,
            "--output-format", "json",
            "--system-prompt", self.style,
            # 只加载 mcp_config 里写的 MCP 服务器（比如记忆库），不带别的
            "--strict-mcp-config",
        ]
        if self.mcp_config:
            args += ["--mcp-config", self.mcp_config]
        # 关掉内置工具，QQ 里来的消息不能在电脑上执行命令或读文件；记忆库照常能用
        args += ["--tools", ""]
        if self.mcp_servers:
            args += ["--allowedTools", *(f"mcp__{name}" for name in self.mcp_servers)]
        if self.model:
            args += ["--model", self.model]
        session_id = self.state.data["sessions"].get(key)
        if session_id:
            args += ["--resume", session_id]

        proc = await asyncio.create_subprocess_exec(
            *args, cwd=WORKDIR,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("claude 超时没回")

        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude 输出看不懂: {err.decode(errors='replace')[-500:] or out[-500:]!r}")
        if result.get("is_error"):
            # 会话失效（比如被清理了）就丢掉旧 session，下次重新开
            self.state.data["sessions"].pop(key, None)
            self.state.save()
            raise RuntimeError(f"claude 报错: {result.get('result')}")

        self.state.data["sessions"][key] = result["session_id"]
        self.state.save()
        return result.get("result", "").strip()


class ApiBackend:
    """调用 Anthropic API，按量计费，可以给群里的人用。"""

    def __init__(self, cfg: dict, style: str, state: State):
        from anthropic import AsyncAnthropic

        api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("API 模式需要在 config.toml 的 [api] 里填 api_key，或者设置 ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = cfg.get("model", "claude-opus-5-5")
        self.max_tokens = cfg.get("max_tokens", 1024)
        self.history_limit = cfg.get("history_limit", 40)
        self.style = style
        self.state = state

    async def reply(self, key: str, text: str) -> str:
        history = self.state.data["histories"].setdefault(key, [])
        history.append({"role": "user", "content": text})
        del history[:-self.history_limit]
        # API 要求第一条必须是 user
        while history and history[0]["role"] != "user":
            history.pop(0)

        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.style,
            messages=history,
        )
        answer = "".join(b.text for b in msg.content if b.type == "text").strip()
        history.append({"role": "assistant", "content": answer})
        self.state.save()
        return answer


def extract_text(event: dict) -> tuple[str, bool]:
    """取出消息里的文字，顺便判断有没有 @ 机器人。兼容数组格式和 CQ 码字符串格式。"""
    self_id = str(event.get("self_id"))
    message = event.get("message")
    if isinstance(message, list):
        parts, at_me = [], False
        for seg in message:
            data = seg.get("data", {})
            if seg.get("type") == "text":
                parts.append(data.get("text", ""))
            elif seg.get("type") == "at" and str(data.get("qq")) == self_id:
                at_me = True
        return "".join(parts).strip(), at_me
    raw = event.get("raw_message") or str(message or "")
    tag = f"[CQ:at,qq={self_id}]"
    return raw.replace(tag, "").strip(), tag in raw


def split_message(text: str) -> list[str]:
    chunks = []
    while len(text) > MAX_SEGMENT:
        cut = text.rfind("\n", 0, MAX_SEGMENT)
        if cut <= 0:
            cut = MAX_SEGMENT
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.owner = int(cfg["owner_qq"])
        self.mode = cfg.get("backend", "subscription")
        self.state = State()
        style = load_style()
        if self.mode == "api":
            self.backend = ApiBackend(cfg.get("api", {}), style, self.state)
            self.groups = {int(g) for g in cfg.get("api", {}).get("groups", [])}
        elif self.mode == "subscription":
            self.backend = SubscriptionBackend(cfg.get("subscription", {}), style, self.state)
            self.groups = set()  # 订阅额度只能自己用，不开群
        else:
            sys.exit(f"backend 只能是 subscription 或 api，现在是 {self.mode!r}")
        self.locks: dict[str, asyncio.Lock] = {}
        self.ws = None

    def route(self, event: dict) -> tuple[str, str, int] | None:
        """决定这条消息要不要回，要回的话返回 (会话 key, 发送动作, 目标号)。"""
        if event.get("post_type") != "message":
            return None
        user_id = int(event.get("user_id", 0))
        if event.get("message_type") == "private":
            if self.mode == "subscription" and user_id != self.owner:
                return None
            return f"private:{user_id}", "send_private_msg", user_id
        if event.get("message_type") == "group":
            group_id = int(event.get("group_id", 0))
            if group_id not in self.groups:
                return None
            return f"group:{group_id}", "send_group_msg", group_id
        return None

    async def send(self, action: str, target: int, text: str):
        id_field = "user_id" if action == "send_private_msg" else "group_id"
        for chunk in split_message(text):
            await self.ws.send(json.dumps({
                "action": action,
                "params": {id_field: target, "message": chunk},
            }, ensure_ascii=False))

    async def handle(self, event: dict):
        routed = self.route(event)
        if not routed:
            return
        key, action, target = routed
        text, at_me = extract_text(event)
        if action == "send_group_msg":
            if not at_me:
                return
            # 群里有好几个人，标一下是谁在说话
            name = event.get("sender", {}).get("card") or event.get("sender", {}).get("nickname") or event.get("user_id")
            text = f"{name}：{text}"
        if not text:
            return
        if text.split("：")[-1].strip() in RESET_COMMANDS:
            self.state.reset(key)
            await self.send(action, target, "好，重新开始。")
            return

        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            log.info("收到 %s: %s", key, text[:80])
            try:
                answer = await self.backend.reply(key, text)
            except Exception as e:
                log.exception("回复失败")
                if action == "send_private_msg":
                    await self.send(action, target, f"出错了：{e}")
                return
            if answer:
                await self.send(action, target, answer)

    async def run(self):
        napcat = self.cfg.get("napcat", {})
        url = napcat.get("ws_url", "ws://127.0.0.1:3001")
        headers = {"Authorization": f"Bearer {napcat['token']}"} if napcat.get("token") else None
        while True:
            try:
                async with connect(url, additional_headers=headers, max_size=None) as ws:
                    self.ws = ws
                    log.info("已连上 NapCat：%s（%s 模式）", url, self.mode)
                    async for raw in ws:
                        event = json.loads(raw)
                        # 带 echo / status 的是动作回执，不是消息
                        if "echo" in event or "status" in event:
                            if event.get("status") == "failed":
                                log.warning("发送失败：%s", event)
                            continue
                        asyncio.create_task(self.handle(event))
            except (OSError, ConnectionClosed) as e:
                log.warning("和 NapCat 断开了（%s），5 秒后重连", e)
                await asyncio.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Bot(load_config()).run())


if __name__ == "__main__":
    main()
