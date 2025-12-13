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

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2.5:1.5b")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", DEFAULT_MODEL)

SYSTEM_PROMPT = (
    "Отвечай на русском. Кратко, структурно, без воды. "
    "Если не уверен — прямо так и скажи."
)

MAX_HISTORY_MESSAGES = 12
WINDOW_SEC = 10
MAX_MSG_PER_WINDOW = 4

# Лимиты входа/контекста (грубая оценка по символам)
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "4000"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "24000"))

# Ограничения для памяти (states)
STATE_TTL_SEC = 24 * 60 * 60  # 24 часа без активности — удалить
MAX_CHAT_STATES = 2000        # жёсткий лимит количества чатов в памяти
STATE_GC_INTERVAL_SEC = 60 * 10  # каждые 10 минут
# ============================================


@dataclass
class ChatState:
    model: str = DEFAULT_MODEL
    history: List[dict] = field(default_factory=list)
    hits: List[float] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)


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
locks: Dict[int, asyncio.Lock] = {}
rl = RateLimiter(WINDOW_SEC, MAX_MSG_PER_WINDOW)
ollama = OllamaClient(OLLAMA_URL)
_gc_task: asyncio.Task | None = None


def get_state(chat_id: int) -> ChatState:
    if chat_id not in states:
        states[chat_id] = ChatState()
    st = states[chat_id]
    st.last_seen = time.time()
    return st


def get_lock(chat_id: int) -> asyncio.Lock:
    lock = locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[chat_id] = lock
    return lock


def gc_states(now: float | None = None) -> int:
    """
    Очищает старые состояния (TTL) и ограничивает общий размер словаря.
    Возвращает количество удалённых записей.
    """
    if now is None:
        now = time.time()

    removed = 0

    # TTL
    cutoff = now - STATE_TTL_SEC
    stale_ids = [cid for cid, st in states.items() if st.last_seen < cutoff]
    for cid in stale_ids:
        states.pop(cid, None)
        removed += 1

    # Лимит по размеру (LRU по last_seen)
    extra = len(states) - MAX_CHAT_STATES
    if extra > 0:
        # удаляем самые старые по last_seen
        for cid, _st in sorted(states.items(), key=lambda kv: kv[1].last_seen)[:extra]:
            states.pop(cid, None)
            removed += 1

    return removed


async def gc_loop():
    while True:
        try:
            removed = gc_states()
            if removed:
                log.info("GC states: removed=%s, alive=%s", removed, len(states))
        except Exception as e:
            log.warning("GC loop error: %s", e)
        await asyncio.sleep(STATE_GC_INTERVAL_SEC)


async def safe_answer(message: Message, text: str):
    try:
        await message.answer(text)
    except Exception as e:
        log.warning("Failed to answer chat_id=%s err=%s", getattr(message.chat, "id", None), e)


def trim_history(hist: List[dict]) -> List[dict]:
    msgs = [m for m in hist if m.get("role") in ("user", "assistant")]
    return msgs[-MAX_HISTORY_MESSAGES:]


def trim_history_by_chars(hist: List[dict], max_chars: int) -> List[dict]:
    """
    Подрезает историю так, чтобы суммарный размер content (user+assistant) не превышал max_chars.
    Идёт с конца (самое новое важнее).
    """
    items = [m for m in hist if m.get("role") in ("user", "assistant")]
    total = 0
    kept: List[dict] = []
    for m in reversed(items):
        content = (m.get("content") or "")
        total += len(content)
        if total > max_chars:
            break
        kept.append(m)
    kept.reverse()
    return kept


def build_messages(state: ChatState, user_text: str) -> List[dict]:
    base = [{"role": "system", "content": SYSTEM_PROMPT}]
    hist = trim_history(state.history)
    # Подрезаем ещё и по символам, чтобы не раздувать контекст
    hist = trim_history_by_chars(hist, MAX_CONTEXT_CHARS)
    return base + hist + [{"role": "user", "content": user_text}]


@dp.startup()
async def on_startup():
    await ollama.start()
    log.info("Bot started. Ollama=%s model=%s", OLLAMA_URL, DEFAULT_MODEL)
    global _gc_task
    if _gc_task is None:
        _gc_task = asyncio.create_task(gc_loop())


@dp.shutdown()
async def on_shutdown():
    await ollama.close()
    global _gc_task
    if _gc_task is not None:
        _gc_task.cancel()
        try:
            await _gc_task
        except asyncio.CancelledError:
            pass
        _gc_task = None
    log.info("Bot stopped")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    try:
        state = get_state(message.chat.id)
        state.history.clear()
        state.model = DEFAULT_MODEL
        await safe_answer(
            message,
            "Готов.\n"
            f"Модель: {state.model}\n"
            "Команды:\n"
            "/model — показать модель\n"
            "/model <name> — сменить модель\n"
            "/reset — сбросить контекст",
        )
    except Exception as e:
        log.exception("cmd_start error: %s", e)
        await safe_answer(message, "Внутренняя ошибка. Попробуй ещё раз.")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    try:
        state = get_state(message.chat.id)
        state.history.clear()
        await safe_answer(message, "Контекст сброшен.")
    except Exception as e:
        log.exception("cmd_reset error: %s", e)
        await safe_answer(message, "Внутренняя ошибка. Попробуй ещё раз.")


@dp.message(Command("model"))
async def cmd_model(message: Message):
    try:
        state = get_state(message.chat.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            await safe_answer(message, f"Текущая модель: {state.model}")
            return
        new_model = parts[1].strip()
        if not new_model:
            await safe_answer(message, f"Текущая модель: {state.model}")
            return
        state.model = new_model
        await safe_answer(message, f"Ок. Модель: {state.model}")
    except Exception as e:
        log.exception("cmd_model error: %s", e)
        await safe_answer(message, "Внутренняя ошибка. Попробуй ещё раз.")


@dp.message(F.text)
async def on_text(message: Message):
    try:
        chat_id = message.chat.id
        # последовательная обработка по чату: защищает историю и порядок ответов
        async with get_lock(chat_id):
            state = get_state(chat_id)

            # периодическая подрезка памяти без ожидания (быстро)
            try:
                if len(states) > MAX_CHAT_STATES:
                    gc_states()
            except Exception:
                pass

            if not rl.allow(state):
                await safe_answer(message, f"Слишком часто. Подожди {WINDOW_SEC} сек.")
                return

            user_text = (message.text or "").strip()
            if not user_text:
                return
            if len(user_text) > MAX_INPUT_CHARS:
                await safe_answer(message, f"Слишком длинное сообщение. Максимум {MAX_INPUT_CHARS} символов.")
                return

            try:
                await message.chat.do("typing")
            except Exception:
                pass

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
                    await safe_answer(message, f"Ошибка Ollama: {str(e2)[:600]}")
                    return

            state.history.append({"role": "user", "content": user_text})
            state.history.append({"role": "assistant", "content": answer})
            state.history = trim_history(state.history)
            # Подрезка по символам тоже, чтобы память не раздувалась “длинными” сообщениями
            state.history = trim_history_by_chars(state.history, MAX_CONTEXT_CHARS)

            await safe_answer(message, (answer or "Пустой ответ.")[:4000])
    except Exception as e:
        log.exception("on_text error: %s", e)
        await safe_answer(message, "Внутренняя ошибка. Попробуй ещё раз.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())