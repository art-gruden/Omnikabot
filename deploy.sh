#!/usr/bin/env bash
# Встановлення й керування ботом на Ubuntu (22.04 / 24.04 / 26.04) та Debian 12+.
#
#   ./deploy.sh                     повне встановлення: Docker, .env, папки, запуск
#   ./deploy.sh --token 123:ABC     те саме без запитань
#   ./deploy.sh update              оновити код (git pull) і перезібрати
#   ./deploy.sh logs | status | restart | stop | start
#   ./deploy.sh doctor              перевірити налаштування й знайти проблему
#   ./deploy.sh token | admins | openai | tz    змінити значення в .env
#   ./deploy.sh backup [тека] | restore ФАЙЛ
#   ./deploy.sh autobackup on|off   щоденна резервна копія о 4:30 (systemd timer)
#   ./deploy.sh uninstall           зупинити й видалити контейнер (дані лишаються)
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
ENV_FILE="$DIR/.env"
PHOTOS_DIR="$DIR/photos"
APP_UID=10001                  # під цим користувачем бот працює в контейнері
SERVICE=bot
BACKUP_DIR_DEFAULT="$DIR/backups"
VOLUME_NAME=""

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
log()  { printf '%s▶%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()   { printf '%s✔%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '%s✖%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

SUDO=""
need_sudo() {
    [ "$(id -u)" -eq 0 ] && return 0
    command -v sudo >/dev/null 2>&1 || die "Потрібні права root: запустіть від root або встановіть sudo."
    SUDO="sudo"
    sudo -n true 2>/dev/null || { log "Потрібен пароль sudo"; sudo -v; }
}

tty_ok() { { : < /dev/tty; } 2>/dev/null; }

ask() {  # ask "підказка" "типове" -> stdout
    local prompt="$1" def="${2:-}" reply=""
    if ! tty_ok; then echo "$def"; return 0; fi
    if [ -n "$def" ]; then printf '%s [%s]: ' "$prompt" "$def" > /dev/tty
    else printf '%s: ' "$prompt" > /dev/tty; fi
    IFS= read -r reply < /dev/tty || true
    echo "${reply:-$def}"
}

ask_secret() {
    local prompt="$1" reply=""
    tty_ok || die "Немає терміналу для введення. Передайте значення аргументом, напр. ./deploy.sh --token 123:ABC"
    printf '%s: ' "$prompt" > /dev/tty
    IFS= read -rs reply < /dev/tty || true
    printf '\n' > /dev/tty
    echo "$reply"
}

compose() { docker compose "$@"; }

# ------------------------------------------------------------------ перевірки системи

check_os() {
    [ -r /etc/os-release ] || die "Не схоже на Linux із /etc/os-release."
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *ubuntu*|*debian*) : ;;
        *) warn "Скрипт розрахований на Ubuntu/Debian, у вас ${PRETTY_NAME:-невідомо}. Спробую далі." ;;
    esac
    OS_ID="${ID:-ubuntu}"; OS_CODENAME="${VERSION_CODENAME:-}"
    [ "$(uname -m)" = "x86_64" ] || [ "$(uname -m)" = "aarch64" ] \
        || warn "Архітектура $(uname -m) — офіційні образи Docker можуть бути недоступні."
}

ensure_swap() {
    local mem_mb swap_mb
    mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
    if [ "$mem_mb" -ge 1800 ] || [ "$swap_mb" -ge 1000 ]; then return 0; fi
    warn "Мало оперативної пам'яті (${mem_mb} МБ) і немає swap — збірка образу може обірватися."
    [ "$(ask 'Створити файл підкачки 2 ГБ? (y/n)' y)" = "y" ] || return 0
    need_sudo
    if [ ! -f /swapfile ]; then
        $SUDO fallocate -l 2G /swapfile 2>/dev/null || $SUDO dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
        $SUDO chmod 600 /swapfile
        $SUDO mkswap /swapfile >/dev/null
    fi
    $SUDO swapon /swapfile 2>/dev/null || true
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
    ok "Swap 2 ГБ увімкнено."
}

install_docker() {
    if docker compose version >/dev/null 2>&1; then
        ok "Docker уже встановлено ($(docker --version | cut -d, -f1))."
        return 0
    fi
    if command -v docker >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
        warn "Docker є, але без плагіна compose — доставлю його."
    fi
    log "Встановлюю Docker з офіційного репозиторію…"
    need_sudo
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq ca-certificates curl gnupg
    $SUDO install -m 0755 -d /etc/apt/keyrings
    if [ ! -s /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" \
            | $SUDO gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg \
            || die "Не вдалося завантажити ключ Docker. Перевірте доступ до мережі."
        $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${OS_ID} ${OS_CODENAME} stable" \
        | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
    if ! $SUDO apt-get update -qq; then
        warn "Репозиторій Docker недоступний — ставлю пакети з репозиторію дистрибутива."
        $SUDO rm -f /etc/apt/sources.list.d/docker.list
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq docker.io docker-compose-v2 \
            || die "Не вдалося встановити Docker. Встановіть його вручну: https://docs.docker.com/engine/install/"
    else
        $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
            || die "Не вдалося встановити Docker."
    fi
    $SUDO systemctl enable --now docker
    docker compose version >/dev/null 2>&1 || die "docker compose не працює після встановлення."
    ok "Docker встановлено й додано в автозапуск."
    if [ "$(id -u)" -ne 0 ] && ! id -nG "$USER" | grep -qw docker; then
        $SUDO usermod -aG docker "$USER"
        warn "Вас додано в групу docker. Щоб користуватися docker без sudo, перезайдіть у систему."
        warn "Зараз скрипт продовжить через sudo."
        DOCKER_NEEDS_SUDO=1
    fi
}

# якщо поточний сеанс ще не в групі docker — працюємо через sudo
fix_docker_access() {
    if docker info >/dev/null 2>&1; then return 0; fi
    need_sudo
    if $SUDO docker info >/dev/null 2>&1; then
        compose() { $SUDO docker compose "$@"; }
        docker() { command $SUDO docker "$@"; }
        warn "Працюю з docker через sudo (після перезаходу в систему це не знадобиться)."
    else
        die "Docker не відповідає. Перевірте: systemctl status docker"
    fi
}

# ------------------------------------------------------------------ .env

env_get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | head -1 || true; }

env_set() {  # env_set KEY VALUE
    local key="$1" val="$2" tmp
    touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}

valid_token() { [[ "$1" =~ ^[0-9]{5,12}:[A-Za-z0-9_-]{30,}$ ]]; }

ensure_env() {
    [ -f "$ENV_FILE" ] || { [ -f .env.example ] && cp .env.example "$ENV_FILE" || touch "$ENV_FILE"; }
    chmod 600 "$ENV_FILE"

    local token="${ARG_TOKEN:-$(env_get BOT_TOKEN)}"
    while ! valid_token "$token"; do
        [ -n "$token" ] && warn "Токен має вигляд 123456789:AAE... — спробуйте ще раз."
        echo "Токен бота беруть у Telegram у @BotFather: /newbot"
        token="$(ask_secret 'Вставте BOT_TOKEN')"
    done
    env_set BOT_TOKEN "$token"

    local admins="${ARG_ADMINS:-$(env_get ADMIN_IDS)}"
    if [ -z "$admins" ]; then
        echo "ID адміністратора (своє число дізнаєтесь у бота командою /id або в @userinfobot)."
        echo "Можна лишити порожнім: тоді адміном стане перший, хто напише боту."
        admins="$(ask 'ADMIN_IDS (через кому)' '')"
    fi
    env_set ADMIN_IDS "$admins"

    local key="${ARG_OPENAI:-$(env_get OPENAI_API_KEY)}"
    if [ -z "$key" ] && [ -z "${ARG_NO_ASK:-}" ]; then
        echo "Ключ OpenAI (для оформлення текстів і малювання картинок) можна ввести пізніше в меню бота."
        if [ "$(ask 'Ввести ключ OpenAI зараз? (y/n)' n)" = "y" ]; then
            key="$(ask_secret 'OPENAI_API_KEY')"
        fi
    fi
    [ -n "$key" ] && env_set OPENAI_API_KEY "$key"

    local tz="${ARG_TZ:-$(env_get TZ)}"
    if [ -z "$tz" ]; then
        tz="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo Europe/Berlin)"
    fi
    env_set TZ "$tz"
    ok ".env готовий (токен збережено, права 600, часовий пояс $tz)."
}

ensure_photos() {
    mkdir -p "$PHOTOS_DIR"
    # бот у контейнері працює під uid 10001 і має могти писати туди фото дня
    if [ "$(stat -c %u "$PHOTOS_DIR")" != "$APP_UID" ]; then
        need_sudo
        $SUDO chown -R "$APP_UID:$APP_UID" "$PHOTOS_DIR" 2>/dev/null || chmod 777 "$PHOTOS_DIR"
    fi
    ok "Папка для фото дня: $PHOTOS_DIR (файли ddmm.jpg, напр. $(date +%d%m).jpg на сьогодні)."
}

# ------------------------------------------------------------------ запуск і перевірка

volume_name() {
    [ -n "$VOLUME_NAME" ] && { echo "$VOLUME_NAME"; return; }
    VOLUME_NAME="$(compose config --format json 2>/dev/null \
        | python3 -c 'import json,sys;d=json.load(sys.stdin);print(next(iter(d.get("volumes",{}))))' 2>/dev/null || true)"
    [ -z "$VOLUME_NAME" ] && VOLUME_NAME="$(basename "$DIR" | tr -cd '[:alnum:]_-')_bot-data"
    echo "$VOLUME_NAME"
}

start_stack() {
    log "Збираю образ і запускаю (перший раз це кілька хвилин)…"
    compose up -d --build
}

wait_started() {
    local deadline=$((SECONDS + 90)) out=""
    log "Чекаю, поки бот підключиться до Telegram…"
    while [ $SECONDS -lt $deadline ]; do
        out="$(compose logs --no-color --tail 200 "$SERVICE" 2>/dev/null || true)"
        if grep -q "запущено" <<<"$out"; then
            ok "$(grep -o 'бот @[^ ]* запущено' <<<"$out" | tail -1)"
            return 0
        fi
        if grep -qi "Unauthorized\|InvalidToken" <<<"$out"; then
            die "Telegram не прийняв токен. Змініть його: ./deploy.sh token"
        fi
        if grep -q "Задайте BOT_TOKEN" <<<"$out"; then
            die "У .env немає BOT_TOKEN. Виконайте: ./deploy.sh token"
        fi
        sleep 2
    done
    warn "За 90 секунд бот не повідомив про запуск. Подивіться логи: ./deploy.sh logs"
    return 1
}

cmd_install() {
    check_os
    install_docker
    fix_docker_access
    ensure_swap
    ensure_env
    ensure_photos
    start_stack
    wait_started || true
    echo
    ok "Готово. Відкрийте бота в Telegram і напишіть /start."
    echo "   Налаштування:        /menu          Рубрики постів:  /post"
    echo "   Ключ ChatGPT:        /menu → 🛠 Адміністрування → 🤖 OpenAI"
    echo "   Фото на день:        покладіть $(date +%d%m).jpg у $PHOTOS_DIR"
    echo "   Логи:                ./deploy.sh logs"
    echo "   Оновлення:           ./deploy.sh update"
    [ -n "${DOCKER_NEEDS_SUDO:-}" ] && warn "Перезайдіть у систему, щоб користуватися docker без sudo."
    return 0
}

cmd_update() {
    fix_docker_access
    if [ -d .git ]; then
        log "Оновлюю код із git…"
        git pull --ff-only || warn "git pull не вдався — оновлюю з наявних файлів."
    fi
    ensure_photos
    compose up -d --build
    wait_started || true
    ok "Оновлено. Налаштування, зразки й шаблони збережено."
}

cmd_backup() {
    fix_docker_access
    local dest="${1:-$BACKUP_DIR_DEFAULT}" vol stamp file
    mkdir -p "$dest"
    vol="$(volume_name)"
    stamp="$(date +%Y%m%d-%H%M%S)"
    file="$dest/omnikabot-$stamp.tar.gz"
    log "Зберігаю дані тому $vol…"
    docker run --rm -v "$vol":/data:ro -v "$(cd "$dest" && pwd)":/out alpine \
        tar czf "/out/$(basename "$file")" -C /data . || die "Не вдалося створити копію."
    [ -d "$PHOTOS_DIR" ] && tar czf "$dest/photos-$stamp.tar.gz" -C "$DIR" photos 2>/dev/null || true
    ok "Копія: $file"
    ls -1t "$dest"/omnikabot-*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f   # лишаємо 10 останніх
}

cmd_restore() {
    local file="${1:-}"
    [ -f "$file" ] || die "Вкажіть файл копії: ./deploy.sh restore backups/omnikabot-….tar.gz"
    fix_docker_access
    [ "$(ask 'Поточні налаштування буде замінено. Продовжити? (y/n)' n)" = "y" ] || exit 0
    compose down
    docker run --rm -v "$(volume_name)":/data -v "$(cd "$(dirname "$file")" && pwd)":/in alpine \
        sh -c "rm -rf /data/* /data/..?* 2>/dev/null; tar xzf /in/$(basename "$file") -C /data"
    compose up -d
    wait_started || true
    ok "Відновлено з $file"
}

cmd_autobackup() {
    local mode="${1:-on}"
    need_sudo
    local unit=/etc/systemd/system/omnikabot-backup
    if [ "$mode" = "off" ]; then
        $SUDO systemctl disable --now omnikabot-backup.timer 2>/dev/null || true
        $SUDO rm -f "$unit.service" "$unit.timer"
        $SUDO systemctl daemon-reload
        ok "Щоденну копію вимкнено."
        return 0
    fi
    $SUDO tee "$unit.service" >/dev/null <<EOF
[Unit]
Description=Резервна копія даних Omnikabot
[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=$DIR/deploy.sh backup
EOF
    $SUDO tee "$unit.timer" >/dev/null <<'EOF'
[Unit]
Description=Щоденна резервна копія Omnikabot
[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now omnikabot-backup.timer
    ok "Щоденна копія о 04:30 у $BACKUP_DIR_DEFAULT (зберігаються 10 останніх)."
}

cmd_doctor() {
    local bad=0
    echo "${BOLD}Перевірка${OFF}"
    if docker compose version >/dev/null 2>&1; then ok "docker compose: $(docker compose version --short 2>/dev/null)"
    else echo "${RED}✖${OFF} docker compose не встановлено — запустіть ./deploy.sh"; bad=1; fi
    fix_docker_access 2>/dev/null || true
    if [ -f "$ENV_FILE" ]; then
        local perm; perm="$(stat -c %a "$ENV_FILE")"
        [ "$perm" = "600" ] && ok ".env є, права $perm" || { warn ".env має права $perm — краще 600 (chmod 600 .env)"; }
        valid_token "$(env_get BOT_TOKEN)" && ok "BOT_TOKEN заповнений" || { echo "${RED}✖${OFF} BOT_TOKEN порожній або неправильний"; bad=1; }
        [ -n "$(env_get ADMIN_IDS)" ] && ok "ADMIN_IDS: $(env_get ADMIN_IDS)" || warn "ADMIN_IDS порожній — адміном стане перший користувач"
        [ -n "$(env_get OPENAI_API_KEY)" ] && ok "Ключ OpenAI заданий у .env" || warn "Ключа OpenAI у .env немає (можна ввести в меню бота)"
        ok "Часовий пояс: $(env_get TZ)"
    else echo "${RED}✖${OFF} немає .env — запустіть ./deploy.sh"; bad=1; fi
    if [ -d "$PHOTOS_DIR" ]; then
        local owner today; owner="$(stat -c %u "$PHOTOS_DIR")"; today="$(date +%d%m)"
        [ "$owner" = "$APP_UID" ] || [ "$(stat -c %a "$PHOTOS_DIR")" = "777" ] \
            && ok "Папка фото доступна боту на запис" \
            || warn "Боту може бракувати прав на $PHOTOS_DIR (sudo chown -R $APP_UID $PHOTOS_DIR)"
        ls "$PHOTOS_DIR/$today".* >/dev/null 2>&1 && ok "Фото на сьогодні є: $today.*" \
            || warn "Фото на сьогодні ($today.jpg) немає — «Єдність сердець» запропонує згенерувати картинку"
    else warn "Немає папки $PHOTOS_DIR"; fi
    if compose ps --status running 2>/dev/null | grep -q "$SERVICE"; then
        ok "Контейнер працює"
        local errs; errs="$(compose logs --no-color --tail 300 "$SERVICE" 2>/dev/null | grep -ci "ERROR" || true)"
        [ "${errs:-0}" -gt 0 ] && warn "У свіжих логах $errs рядків ERROR: ./deploy.sh logs" || ok "Помилок у свіжих логах немає"
    else echo "${RED}✖${OFF} контейнер не запущено (./deploy.sh start)"; bad=1; fi
    echo
    [ "$bad" = 0 ] && ok "Усе гаразд." || warn "Є проблеми — див. вище."
}

usage() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; }

ARG_TOKEN=""; ARG_ADMINS=""; ARG_OPENAI=""; ARG_TZ=""; ARG_NO_ASK=""
CMD=""
while [ $# -gt 0 ]; do
    case "$1" in
        --token)   ARG_TOKEN="${2:-}"; shift 2 ;;
        --admins)  ARG_ADMINS="${2:-}"; shift 2 ;;
        --openai)  ARG_OPENAI="${2:-}"; shift 2 ;;
        --tz)      ARG_TZ="${2:-}"; shift 2 ;;
        --yes|-y)  ARG_NO_ASK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)         if [ -z "$CMD" ]; then CMD="$1"; shift; else break; fi ;;
    esac
done

case "${CMD:-install}" in
    install|"")  cmd_install ;;
    update)      cmd_update ;;
    start)       fix_docker_access; compose up -d; wait_started || true ;;
    stop)        fix_docker_access; compose stop; ok "Зупинено." ;;
    restart)     fix_docker_access; compose restart; wait_started || true ;;
    status)      fix_docker_access; compose ps; echo; docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null | head -5 ;;
    logs)        fix_docker_access; compose logs -f --tail 100 "$SERVICE" ;;
    doctor)      cmd_doctor ;;
    token)       t="${ARG_TOKEN:-$(ask_secret 'Новий BOT_TOKEN')}"; valid_token "$t" || die "Токен має вигляд 123456789:AAE…"
                 env_set BOT_TOKEN "$t"; fix_docker_access; compose up -d; wait_started || true ;;
    admins)      a="${ARG_ADMINS:-$(ask 'ADMIN_IDS через кому' "$(env_get ADMIN_IDS)")}"; env_set ADMIN_IDS "$a"
                 fix_docker_access; compose up -d; ok "ADMIN_IDS=$a" ;;
    openai)      k="${ARG_OPENAI:-$(ask_secret 'OPENAI_API_KEY (порожньо — прибрати)')}"; env_set OPENAI_API_KEY "$k"
                 fix_docker_access; compose up -d; ok "Ключ OpenAI оновлено в .env (у боті він також налаштовується через меню)." ;;
    tz)          z="${ARG_TZ:-$(ask 'Часовий пояс' "$(env_get TZ)")}"; env_set TZ "$z"; fix_docker_access; compose up -d; ok "TZ=$z" ;;
    backup)      cmd_backup "${1:-}" ;;
    restore)     cmd_restore "${1:-}" ;;
    autobackup)  cmd_autobackup "${1:-on}" ;;
    uninstall)   fix_docker_access
                 [ "$(ask 'Зупинити й видалити контейнер? Дані (том) лишаться. (y/n)' n)" = "y" ] || exit 0
                 compose down; ok "Контейнер видалено. Дані: docker volume ls | grep bot-data" ;;
    *)           usage; die "Невідома команда: $CMD" ;;
esac
