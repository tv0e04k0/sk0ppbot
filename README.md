# sk0ppbot

Telegram-бот на **aiogram (polling)** с генерацией ответов через **Ollama**.

## Требования

- Python 3.10+
- Запущенный Ollama (`http://127.0.0.1:11434`)

## Установка

```bash
cd /root/sk0ppbot
python -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## Конфигурация

Создайте файл `.env` рядом с `bot.py`:

- `TELEGRAM_BOT_TOKEN=...`

## Запуск (локально)

```bash
. .venv/bin/activate
python bot.py
```

## Запуск на VPS (рекомендуется через systemd)

Бот на VPS управляется сервисом `sk0ppbot.service`:

```bash
systemctl status sk0ppbot.service
systemctl restart sk0ppbot.service
tail -n 100 /root/sk0ppbot/systemd.log
```

Важно: **не запускайте параллельно `nohup python bot.py ...`**, иначе получите конфликт polling (`TelegramConflictError`).
