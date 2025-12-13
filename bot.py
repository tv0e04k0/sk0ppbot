import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List
import os
from pathlib import Path
from dotenv import load_dotenv

# --- env ---
load_dotenv(Path(__file__).with_name('.env'))
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN is not set')
# ------------


import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# ================== CONFIG ==================

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:1.5b"
FALLBACK_MODEL = "qwen2.5:1.5b"

SYSTEM_PROMPT = (
    "Отвечай на русском. Кратко, структурно, без воды. "
    "Если не уверен — прямо так и скажи."
)

MAX_HISTORY_MESSAGES = 12
WINDOW_SEC = 10
MAX_MSG_PER_WINDOW = 4
# ============================================


@dataclass
class ChatState:
    model: str = DEFAULT_MODEL
    history: List[dict] = field(default_factory=list)
    hits: List[float] = field(default_factory=list)


class RateLimiter:
    def __init__(self, window_sec: int, max_hits: int):
        self.window_sec = window_sec
        self.max_hits = max_hits

    def allow(self, st: ChatState) -> bool:
        now = time.time()
        cutoff = now - self.window_sec
        st.hits[:] = [t for t in st.hits if t >= cutoff]
        if len(st.hits) >= self.max_hits:
            return False
        st.hits.append(now)
        return True


class OllamaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=90)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self.session:
            await self.session.close()

    async def chat(self, model: str, messages: List[dict]) -> str:
        if not self.session:
            raise RuntimeError("HTTP session not started")

        url = f"{self.base_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                async with self.session.post(url, json=payload) as r:
                    if r.status >= 400:
                        body = await r.text()
                        raise RuntimeError(f"Ollama HTTP {r.status}: {body[:300]}")
                    data = await r.json()
                return ((data.get("message") or {}).get("content") or "").strip()
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.6 * attempt)

        raise RuntimeError(f"Ollama error: {last_err}")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("sk0ppbot")

bot = Bot(TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

states: Dict[int, ChatState] = {}
rl = RateLimiter(WINDOW_SEC, MAX_MSG_PER_WINDOW)
ollama = OllamaClient(OLLAMA_URL)


def get_state(chat_id: int) -> ChatState:
    if chat_id not in states:
        states[chat_id] = ChatState()
    return states[chat_id]


def trim_history(hist: List[dict]) -> List[dict]:
    msgs = [m for m in hist if m.get("role") in ("user", "assistant")]
    return msgs[-MAX_HISTORY_MESSAGES:]


def build_messages(state: ChatState, user_text: str) -> List[dict]:
    base = [{"role": "system", "content": SYSTEM_PROMPT}]
    hist = [m for m in state.history if m.get("role") in ("user", "assistant")]
    hist = hist[-MAX_HISTORY_MESSAGES:]
    return base + hist + [{"role": "user", "content": user_text}]


@dp.startup()
async def on_startup():
    await ollama.start()
    log.info("Bot started. Ollama=%s model=%s", OLLAMA_URL, DEFAULT_MODEL)


@dp.shutdown()
async def on_shutdown():
    await ollama.close()
    log.info("Bot stopped")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    state = get_state(message.chat.id)
    state.history.clear()
    state.model = DEFAULT_MODEL
    await message.answer(
        "Готов.\n"
        f"Модель: {state.model}\n"
        "Команды:\n"
        "/model — показать модель\n"
        "/model <name> — сменить модель\n"
        "/reset — сбросить контекст"
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    state = get_state(message.chat.id)
    state.history.clear()
    await message.answer("Контекст сброшен.")


@dp.message(Command("model"))
async def cmd_model(message: Message):
    state = get_state(message.chat.id)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer(f"Текущая модель: {state.model}")
        return
    new_model = parts[1].strip()
    if not new_model:
        await message.answer(f"Текущая модель: {state.model}")
        return
    state.model = new_model
    await message.answer(f"Ок. Модель: {state.model}")


@dp.message(F.text)
async def on_text(message: Message):
    state = get_state(message.chat.id)

    if not rl.allow(state):
        await message.answer(f"Слишком часто. Подожди {WINDOW_SEC} сек.")
        return

    user_text = (message.text or "").strip()
    if not user_text:
        return

    await message.chat.do("typing")
    messages = build_messages(state, user_text)

    try:
        answer = await ollama.chat(state.model, messages)
    except Exception as e:
        log.warning(
            "Primary failed model=%s err=%s; fallback=%s",
            state.model,
            e,
            FALLBACK_MODEL,
        )
        try:
            answer = await ollama.chat(FALLBACK_MODEL, messages)
        except Exception as e2:
            await message.answer(f"Ошибка Ollama: {str(e2)[:600]}")
            return

    state.history.append({"role": "user", "content": user_text})
    state.history.append({"role": "assistant", "content": answer})
    state.history = trim_history(state.history)

    await message.answer((answer or "Пустой ответ.")[:4000])


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())