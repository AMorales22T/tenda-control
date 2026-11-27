#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
app_dir="$data_dir/tenda-control"
applications="$data_dir/applications"
icons="$data_dir/icons/hicolor/scalable/apps"
app_id="com.dolfius.TendaControl"

/usr/bin/python3 - <<'PY'
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("NM", "1.0")
    from gi.repository import Adw, NM
    assert Adw.get_major_version() > 1 or Adw.get_minor_version() >= 4
except (ImportError, ValueError, AssertionError):
    raise SystemExit("Se necesitan GTK 4, libadwaita 1.4 o posterior con libnm y PyGObject para Python 3.")
PY
for tool in pkexec nmcli ip udevadm curl; do
    command -v "$tool" >/dev/null || { echo "Falta la herramienta $tool." >&2; exit 1; }
done

install -d -m 700 "$app_dir"
mkdir -p "$applications" "$icons"
for file in tenda_control.py tenda_helper.py tenda_hardware.py tenda_speedtest.py tenda_networks.py tenda-control.svg; do
    install -m 644 "$project_dir/$file" "$app_dir/$file"
done
install -m 644 "$project_dir/tenda-control.svg" "$icons/$app_id.svg"

/usr/bin/python3 - "$applications/tenda-control.desktop" "$app_dir/tenda_control.py" <<'PY'
import sys
from pathlib import Path

# Exec utiliza dos niveles de escape, uno para la entrada y otro para sus argumentos.
script = sys.argv[2].replace("%", "%%")
for char in ('\\', '"', '`', '$'):
    script = script.replace(char, '\\' + char)
script = script.replace('\\', '\\\\')
Path(sys.argv[1]).write_text(f'''[Desktop Entry]
Type=Application
Name=Control Tenda
Comment=Gestiona el Wi-Fi y Bluetooth del adaptador Tenda
Exec=/usr/bin/python3 "{script}"
Icon=com.dolfius.TendaControl
StartupWMClass=com.dolfius.TendaControl
Terminal=false
Categories=Settings;HardwareSettings;
Keywords=Tenda;Wi-Fi;Bluetooth;USB;
''', encoding='utf-8')
PY
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$applications"
fi
echo "Ya puedes abrir Control Tenda desde el menú de aplicaciones."
