#!/usr/bin/env bash
# 在主机上准备并启动 authentik 栈。可重复执行：已存在的 .env、目录和 systemd 单元不会被覆盖。
set -euo pipefail
umask 077

usage() {
  cat <<'EOF'
用法: sudo scripts/bootstrap.sh [--admin-email EMAIL] [--no-start]

  --admin-email EMAIL  生成新 .env 时写入 akadmin 的邮箱（AUTHENTIK_BOOTSTRAP_EMAIL）
  --no-start           只准备 .env、目录和备份定时器，不启动服务（从备份恢复时使用）

生成新 .env 时会创建 akadmin 初始密码，明文写入 .akadmin-initial-password（权限 600）。
EOF
}

die() { echo "错误: $*" >&2; exit 1; }
warn() { echo "警告: $*" >&2; }
step() { echo "==> $*"; }
rand() { head -c "$1" /dev/urandom | base64 -w0; }

START=1
ADMIN_EMAIL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start) START=0 ;;
    --admin-email) [[ $# -ge 2 ]] || die "--admin-email 需要参数"; ADMIN_EMAIL="$2"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

STACK_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "${STACK_DIR}"

# ---- 1) 主机前置检查 ----
[[ ${EUID} -eq 0 ]] || die "需要以 root 运行（sudo）"
command -v docker >/dev/null || die "未安装 Docker Engine"
docker compose version >/dev/null 2>&1 || die "未安装 Docker Compose v2 插件"
for cmd in ss getent systemctl; do
  command -v "${cmd}" >/dev/null || die "缺少命令: ${cmd}"
done
DOMAIN="$(awk 'NF && $1 !~ /^#/ { print $1; exit }' Caddyfile)"

# ---- 2) .env ----
NEW_ENV=0
if [[ ! -e .env ]]; then
  step "生成 .env"
  tmp="$(mktemp .env.XXXXXX)"
  trap 'rm -f "${tmp}"' EXIT
  sed -e "s|^PG_PASS=.*|PG_PASS=$(rand 36)|" \
      -e "s|^AUTHENTIK_SECRET_KEY=.*|AUTHENTIK_SECRET_KEY=$(rand 60)|" \
      -e "s|^# AUTHENTIK_WEB__BASE_URL=.*|AUTHENTIK_WEB__BASE_URL=https://${DOMAIN}|" \
      .env.example > "${tmp}"
  mv "${tmp}" .env
  trap - EXIT
  NEW_ENV=1
fi
chmod 600 .env
docker compose config --quiet || die ".env 不完整（PG_PASS / AUTHENTIK_SECRET_KEY 不能为空）"

PROJECT="$(docker compose config | awk '/^name:/ { print $2; exit }')"
if [[ ${NEW_ENV} -eq 1 ]] && docker volume inspect "${PROJECT}_database" >/dev/null 2>&1; then
  rm -f .env
  die "数据库卷 ${PROJECT}_database 已存在，新生成的密钥与之不匹配。请先从备份恢复原 .env（RUNBOOK「从备份恢复」）"
fi

# ---- 3) 启动前置检查 ----
if [[ ${START} -eq 1 ]]; then
  if [[ -z "$(docker compose ps --status running --quiet)" ]]; then
    listeners="$(ss -Htlnu '( sport = :80 or sport = :443 )')"
    [[ -z ${listeners} ]] || die "80/443 已被其他进程占用:
${listeners}"
  fi
  resolved="$(getent ahosts "${DOMAIN}" | awk '{ print $1 }' | sort -u | xargs || true)"
  if [[ -z ${resolved} ]]; then
    warn "${DOMAIN} 无法解析，Caddy 将无法签发证书"
  else
    echo "${DOMAIN} 解析到: ${resolved}（请确认是本机公网地址）"
  fi
fi

# ---- 4) 目录 ----
step "准备目录"
# authentik 容器以 uid 1000 运行；worker 不以 root 运行，因此不会自行修正挂载目录属主。
for dir in data certs custom-templates; do
  if [[ ! -d ${dir} ]]; then
    install -d -m 0755 -o 1000 -g 1000 "${dir}"
  elif [[ "$(stat -c %u "${dir}")" != 1000 ]]; then
    warn "${dir}/ 属主不是 uid 1000，authentik 可能无法写入（chown -R 1000:1000 ${dir}）"
  fi
done
for dir in caddy-data caddy-config; do
  [[ -d ${dir} ]] || install -d -m 0700 "${dir}"
done
[[ -d /var/log/caddy ]] || install -d -m 0755 /var/log/caddy

# ---- 5) 镜像与 akadmin 初始密码 ----
step "拉取镜像"
docker compose pull --quiet

if [[ ${NEW_ENV} -eq 1 ]]; then
  step "生成 akadmin 初始密码"
  password="$(rand 24)"
  # authentik 的 hash_password 命令要等数据库就绪；这里直接调用镜像内 Django 的离线哈希。
  hash="$(printf '%s' "${password}" | docker compose run --rm --no-deps -T --entrypoint python server -c '
import sys
from django.conf import settings
settings.configure()
from django.contrib.auth.hashers import make_password
print(make_password(sys.stdin.read()))
')" || die "生成密码哈希失败"
  [[ ${hash} == pbkdf2_sha256\$* ]] || die "密码哈希格式异常"
  # 单引号阻止 compose 对哈希中的 $ 做变量插值。
  printf "\nAUTHENTIK_BOOTSTRAP_PASSWORD_HASH='%s'\n" "${hash}" >> .env
  [[ -z ${ADMIN_EMAIL} ]] || printf 'AUTHENTIK_BOOTSTRAP_EMAIL=%s\n' "${ADMIN_EMAIL}" >> .env
  printf '%s\n' "${password}" > .akadmin-initial-password
  unset password hash
fi

# ---- 6) 备份定时器 ----
step "安装备份定时器"
units_changed=0
for unit in authentik-backup.service authentik-backup.timer; do
  rendered="$(sed "s|/opt/authentik|${STACK_DIR}|g" "systemd/${unit}")"
  target="/etc/systemd/system/${unit}"
  if [[ ! -e ${target} ]]; then
    printf '%s\n' "${rendered}" > "${target}"
    chmod 0644 "${target}"
    units_changed=1
  elif ! diff -q <(printf '%s\n' "${rendered}") "${target}" >/dev/null; then
    warn "${target} 已存在且与仓库版本不同，未覆盖（请 diff 后手动处理）"
  fi
done
[[ ${units_changed} -eq 0 ]] || systemctl daemon-reload
systemctl enable --now authentik-backup.timer

if [[ ${START} -eq 0 ]]; then
  step "准备完毕，未启动服务。继续按 RUNBOOK「从备份恢复」操作。"
  exit 0
fi

# ---- 7) 启动与验证 ----
step "启动服务"
docker compose up -d --wait --wait-timeout 600
docker compose ps

healthy=0
if command -v curl >/dev/null; then
  for _ in $(seq 1 12); do
    if curl -fsS -o /dev/null --max-time 10 "https://${DOMAIN}/-/health/ready/"; then
      healthy=1
      break
    fi
    sleep 10
  done
fi
if [[ ${healthy} -eq 1 ]]; then
  step "https://${DOMAIN}/-/health/ready/ 正常"
else
  warn "未能确认 https://${DOMAIN}/-/health/ready/，检查 DNS、防火墙和 docker compose logs caddy"
fi

if [[ -e .akadmin-initial-password ]]; then
  cat <<EOF

下一步：
  1. 用 akadmin 和 ${STACK_DIR}/.akadmin-initial-password 中的密码登录 https://${DOMAIN}/
  2. 把密码存入密码管理器（或登录后修改），然后执行: shred -u ${STACK_DIR}/.akadmin-initial-password
EOF
fi
