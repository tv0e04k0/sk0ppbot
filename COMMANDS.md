# sk0ppbot — команды управления

## Запуск
cd /root/sk0ppbot
. .venv/bin/activate
nohup python bot.py > bot.log 2>&1 &

## Остановка
pkill -f "/root/sk0ppbot/bot.py"

## Перезапуск
cd /root/sk0ppbot
. .venv/bin/activate
pkill -f bot.py 2>/dev/null || true
nohup python bot.py > bot.log 2>&1 &

## Проверка
ps aux | grep bot.py | grep -v grep

## Логи
tail -n 100 bot.log
tail -f bot.log

## Отладка
cd /root/sk0ppbot
. .venv/bin/activate
python bot.py

## Ollama
systemctl status ollama
curl http://127.0.0.1:11434/api/tags
ollama pull qwen2.5:1.5b

## Обновление кода
cd /root/sk0ppbot
git pull --rebase
