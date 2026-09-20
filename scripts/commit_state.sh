#!/usr/bin/env bash
# Коммитит изменения состояния обратно в репозиторий.
# Репозиторий — это база данных пайплайна (см. docs/ARCHITECTURE.md).
#
# Перед коммитом выполняется чек-лист из docs/SECURITY.md, §е:
#   1. только разрешённые пути;
#   2. каждый state/*.json валиден как JSON;
#   3. в диффе нет похожего на секреты;
#   6. дифф разумного размера.
set -euo pipefail

MESSAGE="${1:-state: автоматическое обновление}"
shift || true
EXTRA_PATHS=("$@")

MAX_DIFF_LINES=2000

git config user.name  "dubai-channel-bot"
git config user.email "bot@users.noreply.github.com"

git add -A state/ "${EXTRA_PATHS[@]}" 2>/dev/null || git add -A state/

if git diff --cached --quiet; then
  echo "Изменений состояния нет — коммит не нужен."
  exit 0
fi

# 1. Разрешены только файлы состояния и реестр источников.
while IFS= read -r path; do
  case "$path" in
    state/*.json|config/sources.yml) ;;
    *)
      echo "ОТКАЗ: попытка закоммитить неразрешённый путь: $path" >&2
      exit 1
      ;;
  esac
done < <(git diff --cached --name-only)

# 2. Каждый изменённый JSON должен парситься.
while IFS= read -r path; do
  case "$path" in
    *.json)
      python -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$path" \
        || { echo "ОТКАЗ: битый JSON: $path" >&2; exit 1; }
      ;;
  esac
done < <(git diff --cached --name-only --diff-filter=d)

# 3. Поиск секретов во всём репозитории.
python scripts/check_no_secrets.py || { echo "ОТКАЗ: найдены похожие на секреты строки." >&2; exit 1; }

# 6. Защита от «бот записал весь дамп апдейтов».
DIFF_LINES="$(git diff --cached --numstat | awk '{added+=$1; removed+=$2} END {print added+removed+0}')"
if [ "$DIFF_LINES" -gt "$MAX_DIFF_LINES" ]; then
  echo "ОТКАЗ: дифф состояния слишком большой ($DIFF_LINES строк > $MAX_DIFF_LINES)." >&2
  echo "Нужна ручная проверка — возможно, в state/ попали лишние данные." >&2
  exit 1
fi

git commit -m "${MESSAGE} [skip ci]"

# Между запуском и коммитом другой workflow мог обновить ветку.
# Workflows сериализованы через concurrency-группу pipeline-state, но ручной
# запуск и плановый всё равно могут пересечься, поэтому push с ретраями.
for attempt in 1 2 3; do
  if git push; then
    echo "Состояние закоммичено."
    exit 0
  fi
  echo "push не прошёл (попытка ${attempt}), подтягиваем изменения…"

  # `set -e` убил бы скрипт прямо посреди rebase и оставил репозиторий
  # в незавершённом состоянии, поэтому конфликт обрабатываем руками.
  if ! git pull --rebase --autostash; then
    echo "ОТКАЗ: конфликт при rebase состояния — откатываем rebase." >&2
    git rebase --abort 2>/dev/null || true
    git stash pop 2>/dev/null || true
    echo "Состояние НЕ сохранено. Перезапустите workflow вручную:" >&2
    echo "  Actions → нужный workflow → Run workflow." >&2
    echo "Повторный запуск безопасен: публикация идемпотентна." >&2
    exit 1
  fi
  sleep $((attempt * 3))
done

echo "Не удалось запушить состояние после 3 попыток." >&2
exit 1
