#!/usr/bin/env python3
"""Helper privilegiado (ejecutado vía pkexec) para controlar Tenda y radios internos."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tenda_hardware import (
    TENDA_BT_IFACES,
    TENDA_PID,
    TENDA_VID,
    TENDA_WIFI_IFACE,
    UDEV_RULE_PATH,
    find_bt_interfaces,
    find_internal_bt_usb,
    find_internal_wifi_iface,
    find_usb_device,
    find_usb_interfaces,
    find_wifi_iface_for_usb,
    get_system_state,
    wifi_service_rule,
)

PROFILES: dict[str, dict[str, bool]] = {
    "dividido": {
        "tenda_wifi": True,
        "tenda_bt": False,
        "internal_wifi": False,
        "internal_bt": True,
    },
    "invertido": {
        "tenda_wifi": False,
        "tenda_bt": True,
        "internal_wifi": True,
        "internal_bt": False,
    },
    "solo_interna": {
        "tenda_wifi": False,
        "tenda_bt": False,
        "internal_wifi": True,
        "internal_bt": True,
    },
    "todo": {
        "tenda_wifi": True,
        "tenda_bt": True,
        "internal_wifi": True,
        "internal_bt": True,
    },
}


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Falló {cmd[0]} (código {result.returncode}).")


def _write_sysfs(path: Path, value: str) -> None:
    path.write_text(f"{value}\n", encoding="utf-8")


def _authorize(ifaces: list[Path], enable: bool) -> None:
    # Un controlador compuesto puede reclamar varias interfaces en el primer probe.
    value = "1" if enable else "0"
    for iface in ifaces:
        if (iface / "authorized").read_text().strip() != value:
            _write_sysfs(iface / "authorized", value)
    if enable:
        for iface in ifaces:
            if not (iface / "driver").exists():
                _write_sysfs(Path("/sys/bus/usb/drivers_probe"), iface.name)


def _tenda_interfaces(numbers: tuple[str, ...]) -> tuple[Path, list[Path]]:
    usb = find_usb_device(TENDA_VID, TENDA_PID)
    if usb is None:
        raise RuntimeError("El adaptador USB Tenda está desconectado.")
    ifaces = find_usb_interfaces(usb, numbers)
    if len(ifaces) != len(numbers):
        raise RuntimeError("El Tenda aún no ha presentado las interfaces necesarias.")
    return usb, ifaces


def set_tenda_bt(enable: bool) -> None:
    _, ifaces = _tenda_interfaces(TENDA_BT_IFACES)
    _authorize(ifaces, enable)


def _enable_wifi(iface: str) -> None:
    _run(["nmcli", "device", "set", iface, "managed", "yes"])
    _run(["ip", "link", "set", iface, "up"])
    _run(["nmcli", "device", "set", iface, "autoconnect", "yes"])


def set_tenda_wifi(enable: bool) -> None:
    usb, ifaces = _tenda_interfaces((TENDA_WIFI_IFACE,))
    _authorize(ifaces, enable)
    if enable:
        _write_sysfs(usb / "power/control", "on")
        for _ in range(30):
            iface = find_wifi_iface_for_usb(usb)
            if iface:
                _enable_wifi(iface)
                return
            time.sleep(0.2)
        raise RuntimeError("El controlador del Tenda no ha creado una interfaz Wi-Fi.")


def set_internal_wifi(enable: bool) -> None:
    iface = find_internal_wifi_iface()
    if not iface:
        raise RuntimeError("No se detecta la tarjeta Wi-Fi interna.")
    if enable:
        _enable_wifi(iface)
    else:
        _run(["nmcli", "device", "set", iface, "managed", "no"])
        _run(["ip", "link", "set", iface, "down"])


def set_internal_bt(enable: bool) -> None:
    usb = find_internal_bt_usb()
    if usb is None:
        raise RuntimeError("No se detecta un Bluetooth interno USB compatible.")
    if enable:
        _write_sysfs(usb / "authorized", "1")
        _write_sysfs(usb / "power/control", "on")
    ifaces = find_bt_interfaces(usb) or find_usb_interfaces(usb, ("00", "01"))
    if ifaces:
        _authorize(ifaces, enable)
    elif not enable:
        _write_sysfs(usb / "authorized", "0")


SETTERS = {
    "tenda_wifi": set_tenda_wifi, "internal_wifi": set_internal_wifi,
    "tenda_bt": set_tenda_bt, "internal_bt": set_internal_bt,
}


def apply_profile(profile_name: str) -> None:
    cfg = PROFILES[profile_name]
    before = get_system_state()
    for target, enabled in cfg.items():
        if enabled and not getattr(before, target).exists:
            raise RuntimeError("Falta un adaptador necesario para este modo.")
    changed = []
    reconnect_iface = None
    try:
        for target, enabled in sorted(cfg.items(), key=lambda item: not item[1]):
            radio = getattr(before, target)
            if not radio.exists:
                continue
            if not enabled and target.endswith("_wifi") and radio.state.split(" ")[0] in ("100", "up"):
                other = "internal_wifi" if target == "tenda_wifi" else "tenda_wifi"
                replacement = getattr(get_system_state(), other)
                if not replacement.iface:
                    raise RuntimeError("La otra interfaz Wi-Fi aún no está disponible.")
                if replacement.state.split(" ")[0] != "100":
                    reconnect_iface = replacement.iface
            if radio.enabled != enabled:
                changed.append(target)
                SETTERS[target](enabled)
    except Exception as exc:
        failed = []
        for target in reversed(changed):
            try:
                SETTERS[target](getattr(before, target).enabled)
            except Exception:
                failed.append(target)
        if failed:
            raise RuntimeError(f"{exc} Revisa el estado de los adaptadores tras el fallo.") from exc
        raise
    if reconnect_iface:
        try:
            _run(["nmcli", "--wait", "5", "device", "wifi", "rescan", "ifname", reconnect_iface])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        try:
            _run(["nmcli", "--wait", "15", "device", "connect", reconnect_iface])
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            raise RuntimeError(
                f"Modo aplicado. NetworkManager no confirmó la conexión de {reconnect_iface}. {exc}"
            ) from exc


def reset_tenda() -> None:
    usb = find_usb_device(TENDA_VID, TENDA_PID)
    if usb is None:
        raise RuntimeError("El adaptador USB Tenda no está conectado.")
    _write_sysfs(usb / "authorized", "0")
    time.sleep(0.8)
    _write_sysfs(usb / "authorized", "1")
    _write_sysfs(usb / "power/control", "on")


def save_default_udev(profile_name: str) -> None:
    cfg = PROFILES[profile_name]
    backup = Path(f"{UDEV_RULE_PATH}.orig")
    if UDEV_RULE_PATH.exists() and not backup.exists():
        shutil.copy2(UDEV_RULE_PATH, backup)

    lines = [
        f"# TENDA_CONTROL_PROFILE={profile_name}",
        "# Gestionado por Control Tenda. Ajustes del adaptador USB.",
        f'ACTION=="add", SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="{TENDA_VID}", ATTR{{idProduct}}=="{TENDA_PID}", ATTR{{power/control}}="on", ATTR{{power/autosuspend}}="-1"',
    ]

    if not cfg["tenda_bt"]:
        lines.append(
            f'ACTION=="add", SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_interface", ATTRS{{idVendor}}=="{TENDA_VID}", ATTRS{{idProduct}}=="{TENDA_PID}", ATTR{{bInterfaceNumber}}=="00", ATTR{{authorized}}="0"'
        )
        lines.append(
            f'ACTION=="add", SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_interface", ATTRS{{idVendor}}=="{TENDA_VID}", ATTRS{{idProduct}}=="{TENDA_PID}", ATTR{{bInterfaceNumber}}=="01", ATTR{{authorized}}="0"'
        )

    if not cfg["tenda_wifi"]:
        lines.append(
            f'ACTION=="add", SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_interface", ATTRS{{idVendor}}=="{TENDA_VID}", ATTRS{{idProduct}}=="{TENDA_PID}", ATTR{{bInterfaceNumber}}=="02", ATTR{{authorized}}="0"'
        )

    service = wifi_service_rule(UDEV_RULE_PATH)
    if profile_name == "dividido" and service:
        lines.append(service)

    previous = UDEV_RULE_PATH.read_bytes() if UDEV_RULE_PATH.exists() else None

    def replace_rule(content: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=UDEV_RULE_PATH.parent, delete=False) as tmp:
            try:
                tmp.write(content)
                tmp.flush()
                os.fchmod(tmp.fileno(), 0o644)
                os.replace(tmp.name, UDEV_RULE_PATH)
            finally:
                Path(tmp.name).unlink(missing_ok=True)

    replace_rule(("\n".join(lines) + "\n").encode())
    try:
        _run(["udevadm", "control", "--reload-rules"])
    except Exception:
        if previous is None:
            UDEV_RULE_PATH.unlink()
        else:
            replace_rule(previous)
        raise


def main() -> int:
    if os.geteuid() != 0:
        print("Este script requiere privilegios de administrador (pkexec).", file=sys.stderr)
        return 1

    if len(sys.argv) < 2:
        print("Uso: tenda_helper.py <profile|set|reset-tenda|save-default> ...", file=sys.stderr)
        return 2

    cmd = sys.argv[1]
    try:
        lock = open("/run/tenda-control.lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Hay otro cambio en curso.") from None
        if cmd == "profile" and len(sys.argv) == 3 and sys.argv[2] in PROFILES:
            apply_profile(sys.argv[2])
        elif cmd == "set" and len(sys.argv) == 4 and sys.argv[3] in ("on", "off"):
            enable = sys.argv[3] == "on"
            target = sys.argv[2]
            if target not in SETTERS:
                raise ValueError(f"Objetivo inválido: {target}")
            SETTERS[target](enable)
        elif cmd == "reset-tenda":
            reset_tenda()
        elif cmd == "save-default" and len(sys.argv) == 3 and sys.argv[2] in PROFILES:
            save_default_udev(sys.argv[2])
        else:
            print("Argumentos inválidos.", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
