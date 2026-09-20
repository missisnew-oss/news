#!/usr/bin/env bash
# Коммитит изменения состояния обратно в репозиторий.
# Репозиторий — это база данных пайплайна (см. docs/ARCHITECTURE.md).
set -euo pipefail

MESSAGE="${1:-state: автоматическое обновление}"
shift || true
EXTRA_PATHS=("$@")

git config user.name  "dubai-channel-bot"
git config user.email "bot@users.noreply.github.com"

git add -A state/ "${EXTRA_PATHS[@]}" 2>/dev/null || git add -A state/

if git diff --cached --quiet; then
  echo "Изменений состояния нет — коммит не нужен."
  exit 0
fi

git commit -m "${MESSAGE} [skip ci]"

# Между запуском и коммитом другой workflow мог обновить ветку.
for attempt in 1 2 3; do
  if git push; then
    echo "Состояние закоммичено."
    exit 0
  fi
  echo "push не прошёл (попытка ${attempt}), подтягиваем изменения…"
  git pull --rebase --autostash
  sleep $((attempt * 3))
done

echo "Не удалось запушить состояние после 3 попыток." >&2
exit 1
