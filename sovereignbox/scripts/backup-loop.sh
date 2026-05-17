#!/bin/sh
# Backup diario de PostgreSQL + tar de media_data.
# Espera hasta las 03:00 (TZ del contenedor = UTC), corre pg_dump y tar, retiene N días.
#
# Una vez por día — usamos un loop con sleep en vez de cron porque el contenedor
# alpine no trae cron por default y usar busybox crond agrega complejidad.

set -e

RETENTION="${RETENTION_DAYS:-30}"
BACKUP_DIR="/backups"
mkdir -p "$BACKUP_DIR"

run_backup() {
  ts=$(date +'%Y%m%d-%H%M%S')
  echo "[$(date -Iseconds)] Iniciando backup $ts"

  # 1. Dump SQL plano (recuperable con psql -f)
  pg_dump --clean --if-exists --format=plain --file="$BACKUP_DIR/db-$ts.sql" \
    || { echo "ERROR: pg_dump falló"; return 1; }
  gzip -9 "$BACKUP_DIR/db-$ts.sql"

  # 2. Tar de media_data (attachments cuando lleguen en Fase 7)
  if [ -d /media_data ] && [ "$(ls -A /media_data 2>/dev/null)" ]; then
    tar -czf "$BACKUP_DIR/media-$ts.tar.gz" -C /media_data . \
      || echo "WARN: tar de media_data falló (continúa)"
  fi

  # 3. Retención
  find "$BACKUP_DIR" -name 'db-*.sql.gz'    -mtime "+$RETENTION" -delete 2>/dev/null || true
  find "$BACKUP_DIR" -name 'media-*.tar.gz' -mtime "+$RETENTION" -delete 2>/dev/null || true

  echo "[$(date -Iseconds)] Backup $ts OK · $(du -sh $BACKUP_DIR/db-$ts.sql.gz | cut -f1)"
}

# Backup inicial al arrancar (útil para verificar que funciona sin esperar 24hs)
sleep 10
run_backup || echo "Backup inicial falló — el loop sigue intentando."

# Loop: esperar hasta las 03:00 UTC y correr backup. Aritmética simple
# para que funcione con BusyBox/alpine `date` (no soporta `-d "tomorrow 03:00"`).
while true; do
  hour=$(date -u +%H)
  minute=$(date -u +%M)
  second=$(date -u +%S)
  # 10# fuerza base 10 (08, 09 son inválidos en octal)
  elapsed=$((10#$hour*3600 + 10#$minute*60 + 10#$second))
  target=$((3*3600))   # 03:00 UTC en segundos desde 00:00
  if [ "$elapsed" -lt "$target" ]; then
    sleep_s=$((target - elapsed))
  else
    sleep_s=$((86400 - elapsed + target))
  fi
  echo "[$(date -Iseconds)] Próximo backup en ${sleep_s}s (~$(($sleep_s / 3600))h$((($sleep_s % 3600) / 60))m)"
  sleep "$sleep_s"
  run_backup || echo "Backup falló — continúa el loop."
done
