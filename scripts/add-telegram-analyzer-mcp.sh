#!/bin/bash
# Автоматическое добавление telegram-bot-analyzer-mcp в проекты с Telegram-ботами
#
# Использование:
#   ./scripts/add-telegram-analyzer-mcp.sh                # обработать текущую директорию
#   ./scripts/add-telegram-analyzer-mcp.sh /path/to/proj  # обработать переданную директорию
#
# Примечание:
# - Скрипт НЕ делает рекурсивный поиск по подпапкам — только 1 проект (папка).
# - Для обновления существующего mcp.json используется Node.js (если доступен).

set -euo pipefail

ANALYZER_NAME="telegram-bot-analyzer"
ANALYZER_URL="http://37.230.117.176:3001/mcp"

is_bot_project() {
  local project_path="$1"

  # Проверка 1: Название проекта содержит "bot"
  local project_name
  project_name="$(basename "$project_path")"
  if [[ "$project_name" == *"bot"* ]] || [[ "$project_name" == *"Bot"* ]]; then
    return 0
  fi

  # Проверка 2: Наличие .env с токеном бота
  if [[ -f "$project_path/.env" ]]; then
    if grep -qiE "BOT_TOKEN|TELEGRAM_TOKEN|TOKEN|BOT_API_KEY" "$project_path/.env" 2>/dev/null; then
      return 0
    fi
  fi

  # Проверка 3: Наличие requirements.txt с библиотеками ботов
  if [[ -f "$project_path/requirements.txt" ]]; then
    if grep -qiE "python-telegram-bot|aiogram|pyTelegramBotAPI|py-telegram-bot-api" "$project_path/requirements.txt" 2>/dev/null; then
      return 0
    fi
  fi

  # Проверка 4: Наличие главного файла бота
  local main_file
  for main_file in bot.py main.py app.py run.py start.py; do
    if [[ -f "$project_path/$main_file" ]]; then
      if grep -qiE "telegram|bot|TelegramBot|Application" "$project_path/$main_file" 2>/dev/null; then
        return 0
      fi
    fi
  done

  return 1
}

add_telegram_analyzer() {
  local project_path="$1"
  local cursor_dir="$project_path/.cursor"
  local mcp_json_path="$cursor_dir/mcp.json"

  mkdir -p "$cursor_dir"

  # Если mcp.json не существует — создаём новый
  if [[ ! -f "$mcp_json_path" ]]; then
    cat >"$mcp_json_path" <<EOF
{
  "mcpServers": {
    "$ANALYZER_NAME": {
      "url": "$ANALYZER_URL"
    }
  }
}
EOF
    echo "  ✅ Создан новый .cursor/mcp.json с $ANALYZER_NAME"
    return 0
  fi

  # Если уже добавлен — ничего не делаем
  if grep -q "$ANALYZER_NAME" "$mcp_json_path" 2>/dev/null; then
    echo "  ℹ️  $ANALYZER_NAME уже настроен"
    return 0
  fi

  # Добавляем через Node.js (надежнее, чем sed/grep для JSON)
  if command -v node >/dev/null 2>&1; then
    node <<NODESCRIPT
const fs = require('fs');
const mcpJsonPath = ${JSON.stringify("$mcp_json_path")};
const analyzerName = ${JSON.stringify("$ANALYZER_NAME")};
const analyzerUrl = ${JSON.stringify("$ANALYZER_URL")};

try {
  const content = fs.readFileSync(mcpJsonPath, 'utf8');
  const json = JSON.parse(content);
  if (!json.mcpServers || typeof json.mcpServers !== 'object') json.mcpServers = {};
  json.mcpServers[analyzerName] = { url: analyzerUrl };
  fs.writeFileSync(mcpJsonPath, JSON.stringify(json, null, 2) + '\n');
  console.log(\`  ✅ \${analyzerName} добавлен в mcp.json\`);
} catch (error) {
  console.error('  ❌ Ошибка обновления mcp.json:', error.message);
  process.exit(1);
}
NODESCRIPT
    return 0
  fi

  echo "  ⚠️  Node.js не найден, не могу обновить mcp.json автоматически"
  echo "  💡 Добавьте вручную в $mcp_json_path:"
  echo "     \"$ANALYZER_NAME\": { \"url\": \"$ANALYZER_URL\" }"
  return 1
}

process_project() {
  local project_path="$1"
  [[ -d "$project_path" ]] || return 1

  if is_bot_project "$project_path"; then
    echo "📁 Найден бот: $project_path"
    add_telegram_analyzer "$project_path"
    return 0
  fi

  return 1
}

main() {
  if [[ -n "${1:-}" ]]; then
    process_project "$1"
    exit 0
  fi

  local current_dir
  current_dir="$(pwd)"
  if process_project "$current_dir"; then
    echo ""
    echo "✅ Telegram Bot Analyzer MCP настроен для текущего проекта"
  else
    echo "ℹ️  Текущий проект не является ботом или уже настроен"
  fi
}

main "$@"


