"""Telegram <-> Claude 桥接：用 Bot API 长轮询收消息，交给 Claude 回复。和 bot.py 共用后端、配置和上下文存档。"""

import asyncio
import json
import logging
import sys
import urllib.request

from bot import RESET_COMMANDS, ApiBackend, State, SubscriptionBackend, load_config, load_style

MAX_SEGMENT = 4000  # Telegram 单条上限 4096 字

log = logging.getLogger("tg-bot")


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


class Telegram:
    def __init__(self, token: str, proxy: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)

    def _call(self, method: str, params: dict, timeout: float):
        req = urllib.request.Request(
            self.base + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败：{data.get('description')}")
        return data["result"]

    async def call(self, method: str, http_timeout: float = 30, **params):
        return await asyncio.to_thread(self._call, method, params, http_timeout)


class TgBot:
    def __init__(self, cfg: dict):
        tg = cfg.get("telegram", {})
        if not tg.get("bot_token"):
            sys.exit("config.toml 的 [telegram] 里要填 bot_token")
        self.owner = int(tg.get("owner_id", 0))
        if not self.owner:
            sys.exit("config.toml 的 [telegram] 里要填 owner_id（你自己的 Telegram 数字 ID）")
        self.tg = Telegram(tg["bot_token"], tg.get("proxy", ""))
        self.mode = cfg.get("backend", "subscription")
        self.state = State()
        style = load_style()
        if self.mode == "api":
            self.backend = ApiBackend(cfg.get("api", {}), style, self.state)
            self.groups = {int(g) for g in tg.get("groups", [])}
        elif self.mode == "subscription":
            self.backend = SubscriptionBackend(cfg.get("subscription", {}), style, self.state)
            self.groups = set()  # 订阅额度只能自己用，不开群
        else:
            sys.exit(f"backend 只能是 subscription 或 api，现在是 {self.mode!r}")
        self.locks: dict[str, asyncio.Lock] = {}
        self.me = None

    def route(self, msg: dict) -> tuple[str, str] | None:
        """决定这条消息要不要回，要回的话返回 (会话 key, 喂给 Claude 的文字)。"""
        text = (msg.get("text") or msg.get("caption") or "").strip()
        user = msg.get("from", {})
        chat = msg.get("chat", {})
        if not text or user.get("is_bot"):
            return None
        if chat.get("type") == "private":
            if self.mode == "subscription" and user.get("id") != self.owner:
                return None
            return f"tg:{chat['id']}", text
        if chat.get("id") not in self.groups:
            return None
        # 群里要 @ 机器人，或者回复机器人的消息才理
        mention = f"@{self.me['username']}"
        replied_to_me = msg.get("reply_to_message", {}).get("from", {}).get("id") == self.me["id"]
        if mention not in text and not replied_to_me:
            return None
        text = text.replace(mention, "").strip()
        name = user.get("first_name") or user.get("username") or user.get("id")
        return f"tg:{chat['id']}", f"{name}：{text}"

    async def send(self, chat_id: int, text: str, reply_to: int | None = None):
        for chunk in split_message(text):
            params = {"chat_id": chat_id, "text": chunk}
            if reply_to:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            await self.tg.call("sendMessage", **params)

    async def keep_typing(self, chat_id: int):
        while True:
            try:
                await self.tg.call("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    async def handle(self, msg: dict):
        routed = self.route(msg)
        if not routed:
            return
        key, text = routed
        chat_id = msg["chat"]["id"]
        is_group = msg["chat"]["type"] != "private"
        reply_to = msg["message_id"] if is_group else None

        if text.split("：")[-1].strip() in RESET_COMMANDS:
            self.state.reset(key)
            await self.send(chat_id, "好，重新开始。", reply_to)
            return

        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            log.info("收到 %s: %s", key, text[:80])
            typing = asyncio.create_task(self.keep_typing(chat_id))
            try:
                answer = await self.backend.reply(key, text)
            except Exception as e:
                log.exception("回复失败")
                if not is_group:
                    await self.send(chat_id, f"出错了：{e}")
                return
            finally:
                typing.cancel()
            if answer:
                await self.send(chat_id, answer, reply_to)

    async def run(self):
        while not self.me:
            try:
                self.me = await self.tg.call("getMe")
            except Exception as e:
                log.warning("连不上 Telegram（%s），10 秒后重试。国内要在 [telegram] 里填 proxy", e)
                await asyncio.sleep(10)
        log.info("已连上 Telegram：@%s（%s 模式）", self.me["username"], self.mode)

        offset = None
        while True:
            try:
                # 长轮询：Telegram 最多挂 50 秒，有新消息立刻返回
                updates = await self.tg.call(
                    "getUpdates", http_timeout=60, offset=offset, timeout=50, allowed_updates=["message"]
                )
            except Exception as e:
                log.warning("拉消息失败（%s），5 秒后重试", e)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    asyncio.create_task(self.handle(update["message"]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(TgBot(load_config()).run())


if __name__ == "__main__":
    main()
