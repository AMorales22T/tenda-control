#!/usr/bin/env python3
"""Detección de hardware (solo lectura) para Tenda AIC 8800D80 y radios internos."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TENDA_VID = "2604"
TENDA_PID = "001f"
TENDA_BT_IFACES = ("00", "01")
TENDA_WIFI_IFACE = "02"

UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-tenda-wifi.rules")


@dataclass
class WifiInfo:
    iface: Optional[str]
    exists: bool
    enabled: bool
    managed: bool
    state: str
    ssid: str
    freq_band: str
    signal: int | None = None


@dataclass
class BtInfo:
    usb_path: Optional[str]
    exists: bool
    enabled: bool
    hci: Optional[str]


@dataclass
class SystemRadioState:
    tenda_connected: bool
    tenda_usb_path: Optional[str]
    tenda_wifi: WifiInfo
    tenda_bt: BtInfo
    internal_wifi: WifiInfo
    internal_bt: BtInfo
    default_profile: str
    has_wifi_service_rule: bool = False
    outbound_iface: str | None = None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def find_usb_device(vid: str, pid: str) -> Optional[Path]:
    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.exists():
        return None
    for entry in sorted(usb_root.iterdir()):
        if ":" in entry.name:
            continue
        if _read_text(entry / "idVendor").lower() == vid and _read_text(entry / "idProduct").lower() == pid:
            return entry
    return None


def find_usb_interfaces(usb_dev: Path, iface_numbers: tuple[str, ...]) -> list[Path]:
    results: list[Path] = []
    prefix = f"{usb_dev.name}:"
    try:
        entries = sorted(usb_dev.iterdir())
    except FileNotFoundError:
        return []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        num = _read_text(entry / "bInterfaceNumber").lower()
        if num in iface_numbers:
            results.append(entry)
    return results


def _is_bt_descriptor(path: Path, prefix: str) -> bool:
    return (
        _read_text(path / f"{prefix}Class").lower() == "e0"
        and _read_text(path / f"{prefix}SubClass").lower() == "01"
        and _read_text(path / f"{prefix}Protocol").lower() == "01"
    )


def find_bt_interfaces(usb_dev: Path) -> list[Path]:
    prefix = f"{usb_dev.name}:"
    try:
        entries = sorted(usb_dev.iterdir())
    except FileNotFoundError:
        return []
    return [
        entry for entry in entries
        if entry.name.startswith(prefix) and _is_bt_descriptor(entry, "bInterface")
    ]


def find_internal_bt_usb() -> Optional[Path]:
    """Detecta el controlador Bluetooth USB interno excluyendo el adaptador Tenda."""
    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.exists():
        return None
    for entry in sorted(usb_root.iterdir()):
        if ":" in entry.name:
            continue
        vid = _read_text(entry / "idVendor").lower()
        pid = _read_text(entry / "idProduct").lower()
        if not vid or (vid == TENDA_VID and pid == TENDA_PID):
            continue
        if _is_bt_descriptor(entry, "bDevice") or find_bt_interfaces(entry):
            return entry
    return None


def _find_hci_for_usb(usb_dev: Optional[Path]) -> Optional[str]:
    if usb_dev is None:
        return None
    bt_root = Path("/sys/class/bluetooth")
    if not bt_root.exists():
        return None
    target_real = usb_dev.resolve()
    for entry in sorted(bt_root.iterdir()):
        if ":" in entry.name:
            continue
        try:
            real = entry.resolve()
        except OSError:
            continue
        if real == target_real or target_real in real.parents:
            return entry.name
    return None


def find_wifi_iface_for_usb(usb_dev: Optional[Path]) -> Optional[str]:
    if usb_dev is None:
        return None
    net_root = Path("/sys/class/net")
    if not net_root.exists():
        return None
    target_real = usb_dev.resolve()
    for entry in sorted(net_root.iterdir()):
        if not (entry / "wireless").exists():
            continue
        dev_link = entry / "device"
        if not dev_link.exists():
            continue
        try:
            real = dev_link.resolve()
        except OSError:
            continue
        if real == target_real or target_real in real.parents:
            return entry.name
    return None


def find_internal_wifi_iface() -> Optional[str]:
    net_root = Path("/sys/class/net")
    if not net_root.exists():
        return None
    candidates: list[str] = []
    for entry in sorted(net_root.iterdir()):
        if not (entry / "wireless").exists():
            continue
        dev_link = entry / "device"
        if not dev_link.exists():
            continue
        try:
            real = str(dev_link.resolve())
        except OSError:
            continue
        if "/usb" not in real:
            candidates.append(entry.name)
    if candidates:
        return candidates[0]
    return None


def _nmcli_device_details(iface: Optional[str]) -> tuple[bool, str, str, str, int | None]:
    """Devuelve (managed, state, ssid, freq_band, signal)."""
    if not iface:
        return False, "desconectado", "", "", None
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "GENERAL.STATE", "device", "show", iface],
            env={**os.environ, "LC_ALL": "C"},
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        oper = _read_text(Path(f"/sys/class/net/{iface}/operstate"))
        flags_raw = _read_text(Path(f"/sys/class/net/{iface}/flags"))
        is_up = False
        try:
            is_up = bool(int(flags_raw, 16) & 1)
        except ValueError:
            pass
        return is_up, oper or ("up" if is_up else "down"), "", "", None

    state_str = out.partition(":")[2].strip()
    code = state_str.split(" ", 1)[0]
    managed = code.isdigit() and int(code) >= 20
    ssid = ""
    freq_band = ""
    signal = None

    if code == "100":
        try:
            wifi_out = subprocess.check_output(
                ["nmcli", "-t", "-f", "IN-USE,SSID,FREQ,SIGNAL", "device", "wifi", "list", "ifname", iface, "--rescan", "no"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            for line in wifi_out.splitlines():
                if line.startswith("*:"):
                    parts = line[2:].rsplit(":", 2)
                    if len(parts) == 3:
                        if parts[2].isdigit() and int(parts[2]) <= 100:
                            signal = int(parts[2])
                        ssid = re.sub(r"\\(.)", r"\1", parts[0])
                        m = re.search(r"(\d+)", parts[1])
                        if m:
                            mhz = int(m.group(1))
                            if mhz >= 5925:
                                freq_band = "6 GHz"
                            elif mhz >= 4900:
                                freq_band = "5 GHz"
                            elif mhz >= 2400:
                                freq_band = "2,4 GHz"
                    break
        except (OSError, subprocess.SubprocessError):
            pass

    return managed, state_str, ssid, freq_band, signal


def wifi_service_rule(rule: Path) -> str:
    """Conserva la integración previa del equipo solo para su propio USB Tenda."""
    for source in (rule, Path(f"{rule}.orig")):
        for line in _read_text(source).splitlines():
            if (line.startswith('ACTION=="add"') and 'SUBSYSTEM=="net"' in line
                    and f'ATTRS{{idVendor}}=="{TENDA_VID}"' in line
                    and f'ATTRS{{idProduct}}=="{TENDA_PID}"' in line
                    and 'ENV{SYSTEMD_WANTS}+="tenda-wifi.service"' in line):
                return line
    return ""


def read_default_profile() -> str:
    if not UDEV_RULE_PATH.exists():
        return "ninguno"
    text = _read_text(UDEV_RULE_PATH)
    m = re.search(r"^#\s*TENDA_CONTROL_PROFILE=(\w+)", text, re.MULTILINE)
    if m:
        return m.group(1)
    if 'ATTR{bInterfaceNumber}=="00", ATTR{authorized}="0"' in text:
        return "dividido"
    return "personalizado"


def outbound_ipv4_iface() -> str | None:
    """Pregunta al núcleo cómo enviaría tráfico público, sin transmitir paquetes."""
    try:
        output = subprocess.check_output(["ip", "-j", "-4", "route", "get", "1.1.1.1"],
                                         text=True, stderr=subprocess.DEVNULL, timeout=2)
        routes = json.loads(output)
        return routes[0].get("dev") if routes else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def get_system_state() -> SystemRadioState:
    tenda_usb = find_usb_device(TENDA_VID, TENDA_PID)
    internal_bt_usb = find_internal_bt_usb()
    internal_wifi_iface = find_internal_wifi_iface()

    # Tenda Wi-Fi
    tenda_wifi_iface = find_wifi_iface_for_usb(tenda_usb)
    wifi_ifaces = []
    tenda_wifi_enabled = False
    if tenda_usb is not None:
        wifi_ifaces = find_usb_interfaces(tenda_usb, (TENDA_WIFI_IFACE,))
        if wifi_ifaces:
            tenda_wifi_enabled = _read_text(wifi_ifaces[0] / "authorized") == "1"
        elif tenda_wifi_iface:
            tenda_wifi_enabled = True
    tw_managed, tw_state, tw_ssid, tw_band, tw_signal = _nmcli_device_details(tenda_wifi_iface)
    tenda_wifi = WifiInfo(
        iface=tenda_wifi_iface,
        exists=bool(wifi_ifaces),
        enabled=tenda_wifi_enabled,
        managed=tw_managed,
        state=tw_state,
        ssid=tw_ssid,
        freq_band=tw_band,
        signal=tw_signal,
    )

    # Tenda Bluetooth
    tenda_bt_hci = _find_hci_for_usb(tenda_usb)
    bt_ifaces = []
    tenda_bt_enabled = False
    if tenda_usb is not None:
        bt_ifaces = find_usb_interfaces(tenda_usb, TENDA_BT_IFACES)
        if bt_ifaces:
            tenda_bt_enabled = all(_read_text(p / "authorized") == "1" for p in bt_ifaces)
        else:
            tenda_bt_enabled = tenda_bt_hci is not None

    tenda_bt = BtInfo(
        usb_path=str(tenda_usb) if tenda_usb else None,
        exists=len(bt_ifaces) == len(TENDA_BT_IFACES),
        enabled=tenda_bt_enabled,
        hci=tenda_bt_hci,
    )

    # Wi-Fi interna
    iw_managed, iw_state, iw_ssid, iw_band, iw_signal = _nmcli_device_details(internal_wifi_iface)
    internal_wifi_enabled = False
    if internal_wifi_iface:
        flags_raw = _read_text(Path(f"/sys/class/net/{internal_wifi_iface}/flags"))
        is_up = False
        try:
            is_up = bool(int(flags_raw, 16) & 1)
        except ValueError:
            pass
        internal_wifi_enabled = is_up and iw_managed

    internal_wifi = WifiInfo(
        iface=internal_wifi_iface,
        exists=internal_wifi_iface is not None,
        enabled=internal_wifi_enabled,
        managed=iw_managed,
        state=iw_state,
        ssid=iw_ssid,
        freq_band=iw_band,
        signal=iw_signal,
    )

    # Bluetooth interno
    internal_bt_hci = _find_hci_for_usb(internal_bt_usb)
    internal_bt_enabled = False
    if internal_bt_usb is not None:
        dev_auth = _read_text(internal_bt_usb / "authorized")
        bt_ifaces = find_bt_interfaces(internal_bt_usb) or find_usb_interfaces(internal_bt_usb, ("00", "01"))
        if bt_ifaces:
            internal_bt_enabled = dev_auth == "1" and any(_read_text(p / "authorized") == "1" for p in bt_ifaces)
        else:
            internal_bt_enabled = dev_auth == "1"

    internal_bt = BtInfo(
        usb_path=str(internal_bt_usb) if internal_bt_usb else None,
        exists=internal_bt_usb is not None,
        enabled=internal_bt_enabled,
        hci=internal_bt_hci,
    )

    return SystemRadioState(
        tenda_connected=tenda_usb is not None,
        tenda_usb_path=str(tenda_usb) if tenda_usb else None,
        tenda_wifi=tenda_wifi,
        tenda_bt=tenda_bt,
        internal_wifi=internal_wifi,
        internal_bt=internal_bt,
        default_profile=read_default_profile(),
        outbound_iface=outbound_ipv4_iface(),
        has_wifi_service_rule=bool(wifi_service_rule(UDEV_RULE_PATH)),
    )
