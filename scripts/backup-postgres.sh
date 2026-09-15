#!/usr/bin/env bash
# 备份 authentik：PostgreSQL dump + .env、compose 配置与持久化目录。由 authentik-backup.timer 每日调用。
set -euo pipefail
umask 077

STACK_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
BACKUP_DIR=/var/backups/authentik
KEEP_DAYS=7

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="${BACKUP_DIR}/postgres/authentik-${TS}.dump"
TAR="${BACKUP_DIR}/files/authentik-files-${TS}.tar.gz"

install -d -m 0700 "${BACKUP_DIR}" "${BACKUP_DIR}/postgres" "${BACKUP_DIR}/files"
trap 'rm -f "${DUMP}.partial" "${TAR}.partial"' EXIT

cd "${STACK_DIR}"

# --- 1) 数据库（custom format，便于 pg_restore 选择性恢复）---
# shellcheck disable=SC2016 # 变量在容器内展开
docker compose exec -T postgresql \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "${DUMP}.partial"

# --- 2) 完整性冒烟检查：非空 + PGDMP 魔数 ---
test -s "${DUMP}.partial"
head -c 5 "${DUMP}.partial" | grep -q PGDMP
mv "${DUMP}.partial" "${DUMP}"

# --- 3) 配置与持久化文件（成员路径相对部署目录）---
tar -czf "${TAR}.partial" -C "${STACK_DIR}" \
  .env \
  compose.yml \
  compose.override.yml \
  Caddyfile \
  data \
  certs \
  custom-templates
mv "${TAR}.partial" "${TAR}"

# --- 4) 保留策略 ---
find "${BACKUP_DIR}/postgres" -name 'authentik-*.dump'       -mtime +"${KEEP_DAYS}" -delete
find "${BACKUP_DIR}/files"    -name 'authentik-files-*.tar.gz' -mtime +"${KEEP_DAYS}" -delete

echo "backup ok ${TS}"
ls -lh "${DUMP}" "${TAR}"
