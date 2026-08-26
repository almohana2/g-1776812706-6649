#!/usr/bin/env bash
# نسخة احتياطية يومية مع الاحتفاظ بآخر N نسخة (SRS §FR-015).
#   ./scripts/backup.sh            نسخة الآن
#   RESTORE=ملف ./scripts/backup.sh --restore   استعادة
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
MONTHLY_KEEP="${BACKUP_MONTHLY_KEEP:-12}"
DB_SERVICE="${DB_SERVICE:-db}"
DB_USER="${POSTGRES_USER:-hydrawise}"
DB_NAME="${POSTGRES_DB:-hydrawise}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/monthly"

if [[ "${1:-}" == "--restore" ]]; then
  : "${RESTORE:?مرّر RESTORE=/path/to/dump.sql.gz}"
  echo "استعادة من $RESTORE إلى قاعدة $DB_NAME — سيُستبدل محتواها."
  read -r -p "اكتب yes للمتابعة: " confirm
  [[ "$confirm" == "yes" ]] || { echo "أُلغيت."; exit 1; }
  gunzip -c "$RESTORE" | docker compose exec -T "$DB_SERVICE" \
    psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1
  echo "تمت الاستعادة."
  exit 0
fi

TARGET="$BACKUP_DIR/daily/${DB_NAME}-${STAMP}.sql.gz"
docker compose exec -T "$DB_SERVICE" \
  pg_dump -U "$DB_USER" -d "$DB_NAME" --clean --if-exists | gzip -9 > "$TARGET"
echo "أُنشئت: $TARGET ($(du -h "$TARGET" | cut -f1))"

# نسخة شهرية في أول يوم من الشهر.
if [[ "$(date +%d)" == "01" ]]; then
  cp "$TARGET" "$BACKUP_DIR/monthly/${DB_NAME}-$(date +%Y%m).sql.gz"
fi

find "$BACKUP_DIR/daily" -name '*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete
ls -1t "$BACKUP_DIR/monthly"/*.sql.gz 2>/dev/null | tail -n "+$((MONTHLY_KEEP + 1))" | xargs -r rm --

# نسخة لا تُختبر ليست نسخة: تحقّق من سلامة الملف الآن لا وقت الكارثة.
gunzip -t "$TARGET" && echo "فحص السلامة: سليم"
