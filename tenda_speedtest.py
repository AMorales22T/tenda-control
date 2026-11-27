#!/usr/bin/env python3
"""Medición manual y cancelable de transferencias HTTPS."""

from __future__ import annotations

import ipaddress
import json
import statistics
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import BinaryIO, Callable

DOWN_URL = "https://speed.cloudflare.com/__down?bytes={size}"
UP_URL = "https://speed.cloudflare.com/__up"
DOWNLOAD_BYTES = 5_000_000
UPLOAD_BYTES = 1_000_000
SYSTEM_ROUTE = "Ruta del sistema"


@dataclass(frozen=True)
class Server:
    name: str
    ping_url: str
    down_url: str
    down_bytes: int
    up_url: str


CLOUDFLARE = Server("Cloudflare", DOWN_URL.format(size=0), DOWN_URL.format(size=DOWNLOAD_BYTES),
                    DOWNLOAD_BYTES, UP_URL)
LIBRESPEED_BASE = "https://ams.speedtest.clouvider.net/backend"
LIBRESPEED = Server("LibreSpeed (Ámsterdam)", f"{LIBRESPEED_BASE}/empty.php",
                    f"{LIBRESPEED_BASE}/garbage.php?ckSize=4", 4 * 1024**2, f"{LIBRESPEED_BASE}/empty.php")
SERVERS = (CLOUDFLARE, LIBRESPEED)


class ServerError(RuntimeError):
    """El servidor de medición no pudo completar la solicitud."""


@dataclass
class SpeedTestResult:
    ping_ms: float
    download_mbps: float
    upload_mbps: float
    via: str
    server: str = CLOUDFLARE.name


def evaluate_speed(result: SpeedTestResult) -> tuple[str, str]:
    if result.download_mbps >= 50.0 and result.upload_mbps >= 15.0 and result.ping_ms <= 60.0:
        return "good", "Muy bien"
    if result.download_mbps >= 15.0 and result.upload_mbps >= 5.0 and result.ping_ms <= 120.0:
        return "fair", "Regular"
    return "bad", "Mal"


def _mbps(num_bytes: int, elapsed_sec: float) -> float:
    if elapsed_sec <= 0:
        return 0.0
    return num_bytes * 8 / (elapsed_sec * 1_000_000)


@dataclass
class Transfer:
    elapsed: float
    downloaded: int
    uploaded: int
    local_ip: str


def _transfer(url: str, *, iface: str | None, cancel: threading.Event | None,
              local_ips: set[str], upload: BinaryIO | None = None) -> Transfer:
    if cancel and cancel.is_set():
        raise InterruptedError("Prueba cancelada.")
    cmd = ["curl", "-q", "--silent", "--show-error", "--fail", "--output", "/dev/null",
           "--connect-timeout", "5", "--max-time", "20", "--write-out",
           "%{http_code} %{time_total} %{size_download} %{size_upload} %{local_ip}"]
    if iface:
        cmd.extend(["--interface", f"if!{iface}"])
    if upload is not None:
        cmd.extend(["--header", "Content-Type: application/octet-stream", "--data-binary", "@-"])
    cmd.append(url)
    with subprocess.Popen(cmd, stdin=upload or subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as proc:
        try:
            while True:
                if cancel and cancel.is_set():
                    raise InterruptedError("Prueba cancelada.")
                try:
                    output, errors = proc.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    if proc.returncode:
        if proc.returncode == 45:
            raise RuntimeError(f"No se pudo usar la interfaz {iface}. Comprueba que esté conectada.")
        if proc.returncode == 28:
            raise ServerError("Se agotó el tiempo de espera.")
        if proc.returncode == 6:
            raise ServerError("El DNS no pudo resolver el nombre del servidor.")
        if proc.returncode == 22:
            status = output.partition(" ")[0]
            raise ServerError(f"El servidor rechazó la solicitud (HTTP {status}).")
        raise ServerError(errors.strip() or f"curl terminó con el código {proc.returncode}.")
    fields = output.split()
    if len(fields) != 5 or not fields[0].startswith("2"):
        raise ServerError("El servidor devolvió una respuesta inesperada.")
    result = Transfer(float(fields[1]), int(fields[2]), int(fields[3]), fields[4])
    local_ips.add(result.local_ip)
    return result


def measure_ping(cancel=None, samples=3, *, iface=None, local_ips=None, server=CLOUDFLARE) -> float:
    addresses = local_ips if local_ips is not None else set()
    return statistics.median(
        _transfer(server.ping_url, iface=iface, cancel=cancel, local_ips=addresses).elapsed * 1000
        for _ in range(samples)
    )


def measure_download(cancel=None, *, iface=None, local_ips=None, server=CLOUDFLARE) -> float:
    result = _transfer(server.down_url, iface=iface, cancel=cancel,
                       local_ips=local_ips if local_ips is not None else set())
    if result.downloaded != server.down_bytes:
        raise RuntimeError("La descarga de prueba llegó incompleta.")
    return _mbps(result.downloaded, result.elapsed)


def measure_upload(cancel=None, *, iface=None, local_ips=None, server=CLOUDFLARE) -> float:
    with tempfile.TemporaryFile() as payload:
        payload.truncate(UPLOAD_BYTES)
        result = _transfer(server.up_url, iface=iface, cancel=cancel, upload=payload,
                           local_ips=local_ips if local_ips is not None else set())
    if result.uploaded != UPLOAD_BYTES:
        raise RuntimeError("La subida de prueba quedó incompleta.")
    return _mbps(result.uploaded, result.elapsed)


def _system_route(local_ips: set[str]) -> str:
    if not local_ips:
        return SYSTEM_ROUTE
    try:
        output = subprocess.check_output(["ip", "-j", "address", "show"], text=True,
                                         stderr=subprocess.DEVNULL, timeout=2)
        devices = json.loads(output)
        names = set()
        for addr in local_ips:
            matches = {dev["ifname"] for dev in devices for info in dev.get("addr_info", [])
                       if ipaddress.ip_address(info["local"]) == ipaddress.ip_address(addr)}
            if len(matches) != 1:
                return SYSTEM_ROUTE
            names.update(matches)
        return ", ".join(sorted(names))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return SYSTEM_ROUTE


def run_speedtest(
    via: str = SYSTEM_ROUTE,
    on_progress: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    *, iface: str | None = None, server: Server | None = None,
) -> SpeedTestResult:
    addresses: set[str] = set()
    candidates = (server,) if server else SERVERS
    failures = []
    for candidate in candidates:
        addresses.clear()
        if on_progress:
            on_progress(f"Latencia HTTPS con {candidate.name}…")
        try:
            latency = measure_ping(cancel, iface=iface, local_ips=addresses, server=candidate)
            break
        except ServerError as exc:
            failures.append(f"{candidate.name}: {exc}")
    else:
        raise ServerError("Falló la medición de latencia. " + " ".join(failures))

    phase = "descarga"
    try:
        if on_progress:
            on_progress(f"Midiendo la descarga con {candidate.name}…")
        down = measure_download(cancel, iface=iface, local_ips=addresses, server=candidate)
        phase = "subida"
        if on_progress:
            on_progress(f"Midiendo la subida con {candidate.name}…")
        up = measure_upload(cancel, iface=iface, local_ips=addresses, server=candidate)
    except RuntimeError as exc:
        raise RuntimeError(f"Falló la {phase} con {candidate.name}. {exc}") from exc

    if cancel and cancel.is_set():
        raise InterruptedError("Prueba cancelada.")
    if iface is None and via == SYSTEM_ROUTE:
        via = _system_route(addresses)
    return SpeedTestResult(latency, down, up, via, candidate.name)
