#!/usr/bin/env bash
# 一次性管理员准备（方案 0004「管理员准备与用户初始化」）。
#
# 检查模式（默认，普通用户即可运行）：按下列顺序核对主机前提并打印每项结果。
# --apply（root）：先完成全部检查；全部兼容且没有未核实项时，依次执行：
#   1. 用 apt 安装缺少的 uidmap、systemd-container、restic、python3-yaml、python3-jsonschema；
#   2. 账号不存在时，用 useradd 创建账号：家目录 /home/<用户>、bash、同名主组、锁定的密码，
#      以及由 useradd 各分配的 65536 个 subuid/subgid；创建后重新检查一遍；
#   3. 为该用户开启 linger；
#   4. 服务根目录不存在时，在其直接父目录中建临时目录，设属主和 0750 后原子改名为服务根目录；
#   5. 复查全部项目。
# 遇到以下情况，在修改之前报告并停止：
#   - cgroup v2、非特权用户命名空间不可用，或用户服务未委派 memory、pids、cpu；
#   - 缺少 docker-ce、docker-ce-rootless-extras、docker-compose-plugin（由管理员单独安装）；
#   - 已有账号不是普通 UID、属于 sudo 或 docker 组、家目录不属于它、shell 不是 /bin/bash、
#     密码未锁定，或其 subuid/subgid 格式无效、与其他账号重叠、合计不足 65536；
#   - 新建账号时 /home 不是只由 root 控制、映射文件不存在、已有同名组或目标家目录；
#   - 服务根目录的直接父目录不存在，或各级父目录不属于 root、可被组和其他用户写入；
#     服务根目录是符号链接，或已被其他账号占用；
#   - --apply 时，脚本副本不属于 root 或可被组和其他用户写入。
# 退出码：0 主机准备已核实；1 有缺失或不兼容；2 有未核实项（例如普通用户无法读取密码锁定状态）。
# 完成后，以专用用户运行 scripts/user-prepare.py。
set -euo pipefail
umask 022

usage() {
  cat <<'HELP'
用法: scripts/host-prepare.sh [--apply] [--user NAME] [--service-dir DIR | --no-service-dir]

默认按顺序核对主机前提并打印每项结果。
退出码：0 主机准备已核实；1 有缺失或不兼容；2 有未核实项。

--apply           以 root 先完成全部检查；全部兼容后依次：安装缺少的系统依赖，创建专用账号
                  （家目录、bash、锁定密码、subuid/subgid），开启 linger，创建服务根目录，复查
--user NAME       专用用户名，默认 authentik
--service-dir DIR 服务根目录，默认 /srv/authentik；其直接父目录须已存在，各级父目录只由 root 可写
--no-service-dir  跳过服务根目录，之后由专用用户在自己的家目录中创建

检查失败即停止的条件见脚本开头的说明。
--apply 从属于 root、组和其他用户不可写的副本运行，例如：
  sudo install -m 0755 -o root -g root scripts/host-prepare.sh /usr/local/sbin/authentik-host-prepare
  sudo /usr/local/sbin/authentik-host-prepare --apply
完成后，以专用用户运行 scripts/user-prepare.py。
HELP
}

ok() { echo "ok      $*"; }
todo() { echo "缺失    $*"; missing=1; }
unknown() { echo "未核实  $*"; unverified=1; }
block() { echo "不兼容  $*" >&2; blocked=1; }
die() { echo "FAIL    $*" >&2; exit 1; }

trusted_path() {
  local path="$1" mode
  while :; do
    [[ ! -L ${path} && -d ${path} || ! -L ${path} && -f ${path} ]] || return 1
    [[ "$(stat -c %u -- "${path}")" == 0 ]] || return 1
    mode="$(stat -c %a -- "${path}")" || return 1
    (( (8#${mode} & 8#022) == 0 )) || return 1
    [[ ${path} == / ]] && return 0
    path="$(dirname -- "${path}")"
  done
}

login_def() { awk -v key="$1" -v def="$2" '$1 == key { v = $2 } END { print (v != "" ? v : def) }' /etc/login.defs; }
# shellcheck disable=SC2016 # dpkg 格式字段。
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed$'; }

# 先校验文件中的全部区间，再判断目标用户的区间；映射只由 useradd 分配，这里只做检查。
subid_status() {
  local file="$1" user="$2" uid="$3"
  if [[ ! -e ${file} ]]; then echo missing; return; fi
  [[ -r ${file} ]] || { echo unreadable; return; }
  awk -F: -v u="${user}" -v id="${uid}" '
    /^[[:space:]]*(#|$)/ { next }
    NF != 3 || $1 == "" || $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ { bad = 1; next }
    {
      if ($2 < 1 || $3 < 1 || $2 + $3 > 4294967295) { bad = 1; next }
      n++; s[n] = $2 + 0; e[n] = $2 + $3; mine[n] = ($1 == u || $1 == id)
      if (mine[n]) total += $3
    }
    END {
      if (bad) { print "invalid"; exit }
      for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++)
        if ((mine[i] || mine[j]) && s[i] < e[j] && s[j] < e[i]) { print "overlap"; exit }
      print total >= 65536 ? "ok" : "missing"
    }' "${file}"
}

check_account() {
  local groups record user_home user_shell status file delegated
  account_exists=0
  if ! getent passwd "${USER_NAME}" >/dev/null; then
    trusted_path /home || block "创建账号前须确认 /home 仅由 root 控制"
    for file in /etc/subuid /etc/subgid; do
      [[ -f ${file} && -r ${file} ]] || block "${file} 须先由管理员初始化，useradd 才能分配映射"
    done
    getent group "${USER_NAME}" >/dev/null && block "同名组已存在，须先确认账号用途"
    [[ ! -e /home/${USER_NAME} && ! -L /home/${USER_NAME} ]] || block "家目录 /home/${USER_NAME} 已存在，须人工确认用途"
    todo "创建 ${USER_NAME}：家目录、bash、锁定密码、由 useradd 分配 subuid/subgid"
    return
  fi
  account_exists=1
  uid="$(id -u -- "${USER_NAME}")"
  [[ ${uid} -ge $(login_def UID_MIN 1000) && ${uid} -le $(login_def UID_MAX 60000) && ${uid} -ne 0 ]] \
    || { block "${USER_NAME} 不是普通用户"; return; }
  groups=" $(id -nG -- "${USER_NAME}") "
  [[ ${groups} != *' sudo '* && ${groups} != *' docker '* ]] || block "账号属于 sudo 或 docker 组"
  record="$(getent passwd "${USER_NAME}")"
  IFS=: read -r _ _ _ _ _ user_home user_shell <<< "${record}"
  [[ ${user_home} == /* && -d ${user_home} && $(stat -Lc %u -- "${user_home}") == "${uid}" ]] \
    || block "家目录须存在且归 ${USER_NAME} 所有"
  [[ ${user_shell} == /bin/bash ]] || block "已有账号须使用 /bin/bash；本脚本不改写账号用途"
  if [[ ${EUID} -eq 0 ]]; then
    if status="$(passwd -S "${USER_NAME}" | awk '{print $2}')"; then
      [[ ${status} == L ]] || block "已有账号密码未锁定；本脚本不自动锁定已有账号"
    else
      unknown "密码锁定状态"
    fi
  else
    unknown "密码锁定状态（需管理员完成只读复查）"
  fi
  for file in /etc/subuid /etc/subgid; do
    status="$(subid_status "${file}" "${USER_NAME}" "${uid}")"
    case "${status}" in
      ok) ok "${file}：有效且不重叠的 ID 总数至少 65536" ;;
      unreadable) unknown "${file} 不可读" ;;
      *) block "${file}：${status}；已有账号的映射需管理员单独处理，避免改变数据属主含义" ;;
    esac
  done
  if [[ -e /var/lib/systemd/linger/${USER_NAME} ]]; then ok "linger"; else todo "开启 linger"; fi
  if delegated="$(systemctl show "user@${uid}.service" -p DelegateControllers --value)"; then
    for controller in memory pids cpu; do
      [[ " ${delegated} " == *" ${controller} "* ]] || block "尚未委派 ${controller}；由管理员处理全局 systemd 设置"
    done
  else
    unknown "用户服务 cgroup 委派"
  fi
}

check_service_dir() {
  [[ -n ${SERVICE_DIR} ]] || return 0
  local parent
  parent="$(dirname -- "${SERVICE_DIR}")"
  if ! trusted_path "${parent}"; then
    block "服务目录的直接父目录须存在，且各级父目录属于 root、组和其他用户不可写"
    return
  fi
  if [[ -L ${SERVICE_DIR} ]]; then
    block "服务根目录不能是符号链接"
  elif [[ -e ${SERVICE_DIR} ]]; then
    if [[ ${account_exists} -eq 1 && -d ${SERVICE_DIR} && $(stat -c %u -- "${SERVICE_DIR}") == "${uid}" ]]; then
      ok "服务根目录归 ${USER_NAME} 所有；内部初始化与权限由用户入口处理"
    else
      block "服务根目录已存在，且属于其他账号或不是目录；请管理员确认用途后处理"
    fi
  else
    todo "创建并授权服务根目录 ${SERVICE_DIR}"
  fi
}

preflight() {
  missing=0; blocked=0; unverified=0; apt_missing=()
  echo '==> 主机前提（全部检查完成后才允许修改）'
  [[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || block "需要 cgroup v2"
  local userns=1 pkg
  [[ ! -r /proc/sys/kernel/unprivileged_userns_clone ]] || userns="$(cat /proc/sys/kernel/unprivileged_userns_clone)"
  [[ ${userns} == 1 && $(cat /proc/sys/user/max_user_namespaces) -gt 0 ]] || block "需管理员启用非特权用户命名空间"
  for pkg in uidmap systemd-container restic python3-yaml python3-jsonschema; do
    if installed "${pkg}"; then ok "${pkg}"; else todo "安装 ${pkg}"; apt_missing+=("${pkg}"); fi
  done
  for pkg in docker-ce docker-ce-rootless-extras docker-compose-plugin; do
    installed "${pkg}" || block "缺少 ${pkg}；Docker 安装须单独处理"
  done
  command -v dockerd-rootless-setuptool.sh >/dev/null || block "缺少 dockerd-rootless-setuptool.sh"
  check_account
  check_service_dir
}

# 在临时目录中完成属主和权限设置后原子改名；中途失败时，最终路径保持不存在，可以直接重跑。
create_service_dir() (
  [[ -n ${SERVICE_DIR} && ! -e ${SERVICE_DIR} ]] || exit 0
  local stage parent
  parent="$(dirname -- "${SERVICE_DIR}")"
  stage="$(mktemp -d "${parent}/.authentik-prepare.XXXXXXXX")"
  trap 'rmdir -- "${stage}" 2>/dev/null || true' EXIT
  chmod 0750 -- "${stage}"
  chown -- "$(id -u -- "${USER_NAME}"):$(id -g -- "${USER_NAME}")" "${stage}"
  mv -T -n -- "${stage}" "${SERVICE_DIR}"
  [[ ! -e ${stage} ]] || die "目标目录在执行期间出现；已停止，未覆盖目标"
)

assert_apply_allowed() {
  [[ ${EUID} -eq 0 ]] || die "--apply 需要 root"
  trusted_path "$(readlink -f -- "${BASH_SOURCE[0]}")" || die "请从 root 持有且不可被他人改写的已审阅副本运行"
}

main() {
  APPLY=0; USER_NAME=authentik; SERVICE_DIR=/srv/authentik
  local dir_option=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --apply) APPLY=1 ;;
      --user) [[ $# -ge 2 ]] || die '--user 需要参数'; USER_NAME="$2"; shift ;;
      --service-dir) [[ $# -ge 2 && ${dir_option} -eq 0 ]] || die '服务目录选项冲突或缺少参数'; SERVICE_DIR="$2"; dir_option=1; shift ;;
      --no-service-dir) [[ ${dir_option} -eq 0 ]] || die '服务目录选项冲突'; SERVICE_DIR=''; dir_option=1 ;;
      -h | --help) usage; return 0 ;;
      *) usage >&2; return 2 ;;
    esac
    shift
  done
  [[ ${USER_NAME} =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die '用户名不合法'
  if [[ -n ${SERVICE_DIR} ]]; then
    [[ ${SERVICE_DIR} == /* && ${SERVICE_DIR} != */../* && ${SERVICE_DIR} != */.. && ${SERVICE_DIR} != */./* && ${SERVICE_DIR} != */. ]] || die '服务目录须为不含 . 或 .. 的绝对路径'
    [[ ! -L ${SERVICE_DIR%/} ]] || die '服务根目录不能是符号链接'
    SERVICE_DIR="${SERVICE_DIR%/}"
    case "${SERVICE_DIR}" in
      '' | /bin | /boot | /dev | /etc | /home | /lib | /lib64 | /opt | /proc | /root | /run | /sbin | /srv | /sys | /tmp | /usr | /var) die '不能使用系统根目录作为服务目录' ;;
    esac
  fi
  if [[ ${APPLY} -eq 1 ]]; then assert_apply_allowed; fi
  preflight
  [[ ${blocked} -eq 0 ]] || return 1
  if [[ ${APPLY} -eq 1 ]]; then
    [[ ${unverified} -eq 0 ]] || { echo '结果：有未核实项，未执行修改。'; return 2; }
    if [[ ${#apt_missing[@]} -gt 0 ]]; then
      apt-get update -q
      DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${apt_missing[@]}"
    fi
    if [[ ${account_exists} -eq 0 ]]; then
      useradd --create-home --home-dir "/home/${USER_NAME}" --shell /bin/bash --user-group \
        -K SUB_UID_COUNT=65536 -K SUB_GID_COUNT=65536 "${USER_NAME}"
      # 原生工具负责分配；创建结果有问题时不继续授权目录或启用用户服务。
      preflight
      [[ ${blocked} -eq 0 && ${unverified} -eq 0 ]] || return 1
    fi
    if [[ ! -e /var/lib/systemd/linger/${USER_NAME} ]]; then loginctl enable-linger "${USER_NAME}"; fi
    create_service_dir
    echo '==> 主机准备复查'
    preflight
  fi
  [[ ${blocked} -eq 0 && ${missing} -eq 0 ]] || { echo '结果：主机准备尚未完成。'; return 1; }
  [[ ${unverified} -eq 0 ]] || { echo '结果：可见项满足，但有未核实项。'; return 2; }
  echo "结果：主机准备已核实。后续以 ${USER_NAME} 运行 scripts/user-prepare.py，完成用户目录和 rootless Docker 初始化。"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
