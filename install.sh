#!/usr/bin/env bash
# Installs Dell BIOS Settings (DellBiosGTK).
#
#   ./install.sh                 install for the current user into ~/.local
#   ./install.sh --system        install for everyone into /usr/local (asks for sudo)
#   ./install.sh --prefix DIR    install into DIR
#   ./install.sh --uninstall     remove what an install with the same options put in place
#   ./install.sh --check         only check whether this computer is supported
#
# The helper that talks to the BIOS runs as root (through pkexec). With
# --system it is installed root-owned, which is the safer choice: in a user
# install any program running as you could modify the helper before you next
# authenticate.
set -euo pipefail

NAME=dell-bios-gtk
HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX="$HOME/.local"
SUDO=""
UNINSTALL=0
CHECK_ONLY=0
SYSFS=/sys/class/firmware-attributes/dell-wmi-sysman

while [ $# -gt 0 ]; do
    case "$1" in
        --system) PREFIX=/usr/local; SUDO=sudo ;;
        --prefix) PREFIX=${2:?--prefix needs a directory}; shift ;;
        --uninstall) UNINSTALL=1 ;;
        --check) CHECK_ONLY=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# A prefix the user cannot write needs sudo even without --system.
if [ -z "$SUDO" ] && [ "$(id -u)" -ne 0 ]; then
    probe=$PREFIX
    while [ ! -e "$probe" ]; do probe=$(dirname "$probe"); done
    [ -w "$probe" ] || SUDO=sudo
fi
[ "$(id -u)" -eq 0 ] && SUDO=""

SHARE="$PREFIX/share/$NAME"
BIN="$PREFIX/bin/$NAME"
DESKTOP="$PREFIX/share/applications/$NAME.desktop"

ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$*"; }

check_system() {
    local fine=0
    echo "Checking this computer:"
    vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)
    product=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)
    bios=$(cat /sys/class/dmi/id/bios_version 2>/dev/null || true)
    case "$vendor" in
        *Dell*|*Alienware*) ok "$vendor $product (BIOS $bios)" ;;
        *) bad "not a Dell or Alienware computer (${vendor:-unknown vendor})"; fine=1 ;;
    esac
    if [ -d "$SYSFS/attributes" ]; then
        n=$(find "$SYSFS/attributes" -mindepth 1 -maxdepth 1 -type d | wc -l)
        ok "BIOS settings interface found ($n settings)"
    elif modinfo dell-wmi-sysman >/dev/null 2>&1; then
        if lsmod | grep -q '^dell_wmi_sysman'; then
            bad "dell-wmi-sysman is loaded but found no BIOS settings: this BIOS does not offer the interface"
        else
            warn "dell-wmi-sysman is not loaded; try: sudo modprobe dell-wmi-sysman"
        fi
        fine=1
    else
        bad "this kernel has no dell-wmi-sysman driver (needs Linux 5.11 or newer with CONFIG_DELL_WMI_SYSMAN)"
        fine=1
    fi
    return $fine
}

check_deps() {
    local missing=0
    echo "Checking requirements:"
    if command -v python3 >/dev/null; then ok "python3"; else bad "python3 is missing"; missing=1; fi
    if python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
        ok "PyGObject with GTK 3"
    else
        bad "PyGObject / GTK 3 is missing"
        missing=1
    fi
    if command -v pkexec >/dev/null; then ok "pkexec (polkit)"; else bad "pkexec (polkit) is missing"; missing=1; fi
    if [ $missing -ne 0 ]; then
        echo
        echo "Install the missing packages, for example:"
        if command -v pacman >/dev/null; then echo "  sudo pacman -S --needed python python-gobject gtk3 polkit"
        elif command -v apt-get >/dev/null; then echo "  sudo apt install python3 python3-gi gir1.2-gtk-3.0 policykit-1"
        elif command -v dnf >/dev/null; then echo "  sudo dnf install python3 python3-gobject gtk3 polkit"
        elif command -v zypper >/dev/null; then echo "  sudo zypper install python3 python3-gobject-Gdk typelib-1_0-Gtk-3_0 polkit"
        else echo "  python3, PyGObject (GTK 3 typelib) and polkit"
        fi
    fi
    return $missing
}

refresh_menus() {
    command -v update-desktop-database >/dev/null &&
        $SUDO update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
}

if [ $CHECK_ONLY -eq 1 ]; then
    check_system && echo "This computer is supported." || { echo "This computer is not supported (see above)."; exit 1; }
    exit 0
fi

if [ $UNINSTALL -eq 1 ]; then
    echo "Removing $NAME from $PREFIX"
    $SUDO rm -rf "$SHARE"
    $SUDO rm -f "$BIN" "$DESKTOP"
    refresh_menus
    ok "removed"
    exit 0
fi

check_deps || exit 1
check_system || warn "installing anyway; the program will tell you what is missing when it starts"

echo "Installing into $PREFIX${SUDO:+ (with sudo)}"
$SUDO install -d "$SHARE/dellbiosgtk" "$PREFIX/bin" "$PREFIX/share/applications"
$SUDO install -m 644 "$HERE/dellbiosgtk/__init__.py" "$SHARE/dellbiosgtk/"
$SUDO install -m 755 "$HERE/dellbiosgtk/app.py" "$HERE/dellbiosgtk/helper.py" "$SHARE/dellbiosgtk/"
ok "program files in $SHARE"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<EOF
#!/bin/sh
exec python3 "$SHARE/dellbiosgtk/app.py" "\$@"
EOF
$SUDO install -m 755 "$tmp" "$BIN"
ok "launcher $BIN"

sed "s|^Exec=.*|Exec=$BIN|" "$HERE/dell-bios-gtk.desktop" > "$tmp"
$SUDO install -m 644 "$tmp" "$DESKTOP"
refresh_menus
ok "menu entry \"Dell BIOS Settings\""

case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) warn "$PREFIX/bin is not on your PATH; start it from the menu or with $BIN" ;;
esac
[ -z "$SUDO" ] && [ "$PREFIX" = "$HOME/.local" ] &&
    warn "user install: the root helper is writable by your account; ./install.sh --system is safer"
echo "Done. Start it from the application menu or run: $NAME"
