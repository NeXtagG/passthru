#!/usr/bin/env bash
# Passthru installer. Ставит всё нужное для single-GPU passthrough и настраивает
# сеть/firewall, чтобы у VM был интернет. Запуск:  bash install.sh
# Пароль sudo спросит сам. Повторный запуск безопасен.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"

say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] && die "Запусти БЕЗ sudo (скрипт сам попросит пароль): bash install.sh"
command -v sudo >/dev/null || die "Нужен sudo."

# ---- 1. определить дистрибутив ----
. /etc/os-release 2>/dev/null || die "Не найден /etc/os-release — неизвестная система."
ID_ALL=" ${ID:-} ${ID_LIKE:-} "
say "Система: ${PRETTY_NAME:-$ID}"

PKG="" ; INSTALL="" ; PKGS=""
if   command -v pacman  >/dev/null; then
    PKG=pacman
    INSTALL="sudo pacman -S --needed --noconfirm"
    # iptables-nft не указываем явно: libvirt тянет его сам, а явная установка
    # конфликтует с iptables и ломает --noconfirm.
    PKGS="qemu-full edk2-ovmf swtpm libvirt dnsmasq python-gobject libadwaita gtk4"
elif command -v apt-get >/dev/null; then
    PKG=apt
    INSTALL="sudo apt-get install -y"
    sudo apt-get update
    PKGS="qemu-system-x86 qemu-utils ovmf swtpm libvirt-daemon-system libvirt-clients dnsmasq-base \
          python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ufw"
elif command -v dnf     >/dev/null; then
    PKG=dnf
    INSTALL="sudo dnf install -y"
    PKGS="qemu-kvm edk2-ovmf swtpm libvirt virt-install python3-gobject libadwaita gtk4"
elif command -v zypper  >/dev/null; then
    PKG=zypper
    INSTALL="sudo zypper install -y"
    PKGS="qemu-x86 qemu-ovmf-x86_64 qemu-tools swtpm libvirt libvirt-client dnsmasq \
          python3-gobject python3-gobject-Gdk libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1"
else
    die "Не нашёл поддерживаемый пакетный менеджер (pacman/apt/dnf/zypper). Поставь пакеты вручную — см. README."
fi

# ---- 2. поставить пакеты ----
say "Ставлю пакеты ($PKG): $PKGS"
# shellcheck disable=SC2086
$INSTALL $PKGS || die "Установка пакетов не удалась. Проверь вывод выше."

# ---- 3. libvirt: включить службу/сокеты ----
say "Включаю libvirt"
if systemctl list-unit-files | grep -q '^libvirtd\.service'; then
    sudo systemctl enable --now libvirtd.service
    sudo systemctl enable --now libvirtd.socket 2>/dev/null || true
elif systemctl list-unit-files | grep -q '^virtqemud\.service'; then   # модульный libvirt
    sudo systemctl enable --now virtqemud.socket virtnetworkd.socket virtnodedevd.socket 2>/dev/null || true
    sudo systemctl enable --now virtqemud.service 2>/dev/null || true
else
    warn "Не нашёл службу libvirtd/virtqemud — проверь установку libvirt."
fi

# ---- 4. группа libvirt ----
GROUP=libvirt
getent group libvirt >/dev/null || GROUP=libvirtd
if ! id -nG "$REAL_USER" | tr ' ' '\n' | grep -qx "$GROUP"; then
    say "Добавляю $REAL_USER в группу $GROUP"
    sudo usermod -aG "$GROUP" "$REAL_USER"
    NEED_RELOGIN=1
fi

# ---- 5. сеть по умолчанию + firewall ----
say "Сеть libvirt и firewall"
if ! sudo virsh net-info default >/dev/null 2>&1; then
    sudo virsh net-define /usr/share/libvirt/networks/default.xml 2>/dev/null || \
    sudo virsh net-define /etc/libvirt/qemu/networks/default.xml   2>/dev/null || \
        warn "Сеть default не найдена — создай её в приложении на вкладке Network."
fi
sudo virsh net-autostart default 2>/dev/null || true
sudo virsh net-start     default 2>/dev/null || true
# правила форвардинга/NAT под активный firewall (ufw/firewalld/nftables)
if ( cd "$HERE" && /usr/bin/python3 -m passthru --print-netsetup ) >/tmp/passthru-net.sh 2>/dev/null; then
    sudo bash /tmp/passthru-net.sh || warn "Настройка сети вернула ошибку — проверь вывод."
    rm -f /tmp/passthru-net.sh
fi

# ---- 6. диспетчер хуков libvirt ----
if [ ! -x /etc/libvirt/hooks/qemu ]; then
    say "Ставлю диспетчер хуков libvirt"
    sudo install -Dm755 "$HERE/passthru/hook-dispatcher" /etc/libvirt/hooks/qemu 2>/dev/null || \
        sudo tee /etc/libvirt/hooks/qemu >/dev/null <<'DISP'
#!/bin/bash
VM_NAME="$1"; VM_ACTION="$2"; VM_PHASE="$3"
HOOK_DIR="/etc/libvirt/hooks/qemu.d/$VM_NAME/$VM_ACTION/$VM_PHASE"
[ -d "$HOOK_DIR" ] || exit 0
for s in "$HOOK_DIR"/*; do [ -x "$s" ] || continue; "$s" "$@" || exit $?; done
DISP
    sudo chmod 755 /etc/libvirt/hooks/qemu
    sudo systemctl try-restart libvirtd.service virtqemud.service 2>/dev/null || true
fi

# ---- 7. ярлык в меню ----
say "Ярлык приложения"
APPDIR="$REAL_HOME/.local/share/applications"
run_as_user() { sudo -u "$REAL_USER" "$@"; }
run_as_user mkdir -p "$APPDIR"
run_as_user tee "$APPDIR/passthru.desktop" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=Passthru
Comment=Single-GPU passthrough VM builder
Exec=$HERE/passthru.sh
Icon=video-display
Terminal=false
Categories=System;Emulator;
EOF

# ---- 8. проверка без запуска VM ----
say "Проверка окружения (ничего не запускается)"
run_as_user bash -c "cd '$HERE' && /usr/bin/python3 -m passthru --selftest" 2>/dev/null \
    | sed -n '/== Хост/,/прошли/p' || true

say "Готово."
echo "  Запуск:   $HERE/passthru.sh   (или «Passthru» в меню приложений)"
[ -n "${NEED_RELOGIN:-}" ] && warn "Перелогинься (или перезагрузись) — ты добавлен в группу $GROUP."
