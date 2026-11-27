"""Pruebas de detección y conmutación sin modificar las radios del sistema."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import tenda_hardware as hw
import tenda_helper as helper
import tenda_speedtest as speedtest


def radios():
    return hw.SystemRadioState(
        True, "/usb/tenda",
        hw.WifiInfo("wlx123", True, True, True, "30 (disconnected)", "", ""),
        hw.BtInfo("/usb/tenda", True, True, "hci1"),
        hw.WifiInfo("wlp7s0", True, True, True, "100 (connected)", "Casa", "5 GHz"),
        hw.BtInfo("/usb/internal", True, True, "hci0"), "todo",
    )


class TendaTests(unittest.TestCase):
    def test_red_con_dos_puntos_y_senal_cero(self):
        responses = ["GENERAL.STATE:100 (connected)", r"*:Mi\:casa:5180 MHz:0"]
        with patch.object(hw.subprocess, "check_output", side_effect=responses):
            self.assertEqual(hw._nmcli_device_details("wlx123"),
                             (True, "100 (connected)", "Mi:casa", "5 GHz", 0))

    def test_no_hace_rollback_del_usb_si_falla_reconexion_del_relevo(self):
        state = radios()
        state.tenda_wifi.enabled = False
        setters = {key: Mock() for key in helper.SETTERS}
        with patch.object(helper, "get_system_state", return_value=state), \
             patch.dict(helper.SETTERS, setters), \
             patch.object(helper, "_run", side_effect=[None, RuntimeError("Sin red")]) as run:
            with self.assertRaisesRegex(RuntimeError, "Modo aplicado.*wlx123.*Sin red"):
                helper.apply_profile("dividido")
        self.assertEqual(run.call_args_list, [
            call(["nmcli", "--wait", "5", "device", "wifi", "rescan", "ifname", "wlx123"]),
            call(["nmcli", "--wait", "15", "device", "connect", "wlx123"]),
        ])
        setters["internal_wifi"].assert_called_once_with(False)
        self.assertEqual(setters["tenda_wifi"].call_args_list, [call(True)])

    def test_no_desactiva_wifi_si_falta_interfaz_de_relevo(self):
        state = radios()
        state.tenda_wifi.enabled = False
        state.tenda_wifi.iface = None
        setters = {key: Mock() for key in helper.SETTERS}
        with patch.object(helper, "get_system_state", return_value=state), \
             patch.dict(helper.SETTERS, setters):
            with self.assertRaisesRegex(RuntimeError, "no está disponible"):
                helper.apply_profile("dividido")
        setters["internal_wifi"].assert_not_called()
        self.assertEqual(setters["tenda_wifi"].call_args_list, [call(True), call(False)])

    def test_modo_sin_usb(self):
        state = radios()
        state.tenda_wifi.exists = state.tenda_bt.exists = False
        state.internal_wifi.enabled = False
        setters = {key: Mock() for key in helper.SETTERS}
        with patch.object(helper, "get_system_state", return_value=state), patch.dict(helper.SETTERS, setters):
            with self.assertRaises(RuntimeError):
                helper.apply_profile("dividido")
            helper.apply_profile("solo_interna")
        setters["internal_wifi"].assert_called_once_with(True)
        setters["tenda_wifi"].assert_not_called()

    def test_activa_wifi_con_nombre_asignado_por_linux(self):
        with patch.object(helper, "_tenda_interfaces", return_value=(Path("/usb/tenda"), [])), \
             patch.object(helper, "find_wifi_iface_for_usb", return_value="wlx987"), \
             patch.object(helper, "_write_sysfs"), patch.object(helper, "_run") as run:
            helper.set_tenda_wifi(True)
        self.assertIn(call(["ip", "link", "set", "wlx987", "up"]), run.call_args_list)

    def test_regla_udev_se_restaura_si_falla_la_recarga(self):
        with tempfile.TemporaryDirectory() as folder:
            rule = Path(folder) / "99-tenda.rules"
            rule.write_text("# anterior\n")
            with patch.object(helper, "UDEV_RULE_PATH", rule), \
                 patch.object(helper, "_run", side_effect=RuntimeError("udevadm")):
                with self.assertRaises(RuntimeError):
                    helper.save_default_udev("dividido")
            self.assertEqual(rule.read_text(), "# anterior\n")
            self.assertEqual(Path(f"{rule}.orig").read_text(), rule.read_text())


    def test_detecta_wifi_cuando_device_apunta_al_usb_directo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            usb_dev = root / "usb3" / "3-1"
            usb_dev.mkdir(parents=True)
            net_dir = root / "net" / "wlan0"
            (net_dir / "wireless").mkdir(parents=True)
            (net_dir / "device").symlink_to(usb_dev)
            with patch.object(hw, "Path", side_effect=lambda p: (root / "net") if p == "/sys/class/net" else Path(p)):
                self.assertEqual(hw.find_wifi_iface_for_usb(usb_dev), "wlan0")


    def test_speedtest_calcula_mbps_y_devuelve_resultado(self):
        self.assertAlmostEqual(speedtest._mbps(1_250_000, 1.0), 10.0)
        with patch.object(speedtest, "measure_ping", return_value=18.4), \
             patch.object(speedtest, "measure_download", return_value=125.6), \
             patch.object(speedtest, "measure_upload", return_value=42.3):
            res = speedtest.run_speedtest("Tenda (wlan0)")
        self.assertEqual(res.via, "Tenda (wlan0)")
        self.assertAlmostEqual(res.ping_ms, 18.4)
        self.assertAlmostEqual(res.download_mbps, 125.6)
        self.assertAlmostEqual(res.upload_mbps, 42.3)
        self.assertEqual(speedtest.evaluate_speed(res), ("good", "Muy bien"))
        self.assertEqual(
            speedtest.evaluate_speed(speedtest.SpeedTestResult(80.0, 25.0, 8.0, "Tenda")),
            ("fair", "Regular"),
        )
        self.assertEqual(
            speedtest.evaluate_speed(speedtest.SpeedTestResult(180.0, 10.0, 2.0, "Tenda")),
            ("bad", "Mal"),
        )


    def test_prueba_completa_por_interfaz_con_transferencias_simuladas(self):
        with tempfile.TemporaryDirectory() as folder:
            curl = Path(folder) / "curl"
            log = Path(folder) / "llamadas"
            curl.write_text("""#!/usr/bin/python3
import json, os, sys
with open(os.environ["LOG_CURL"], "a") as log:
    print(json.dumps(sys.argv[1:]), file=log)
url = sys.argv[-1]
down = int(url.partition("bytes=")[2] or 0)
up = len(sys.stdin.buffer.read())
print("200", "0.5", down, up, "192.0.2.5")
""")
            curl.chmod(0o700)
            progress = []
            with patch.dict(os.environ, {"PATH": folder, "LOG_CURL": str(log)}):
                result = speedtest.run_speedtest("Tenda (wlx123)", progress.append, iface="wlx123")
            self.assertEqual(result, speedtest.SpeedTestResult(500, 80, 16, "Tenda (wlx123)"))
            self.assertEqual(len(progress), 3)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 5)
            for args in calls:
                self.assertEqual(args[args.index("--interface") + 1], "if!wlx123")

    def test_servidor_alternativo_usa_la_misma_interfaz(self):
        with patch.object(speedtest, "measure_ping", side_effect=[speedtest.ServerError("HTTP 403"), 24]) as ping, \
             patch.object(speedtest, "measure_download", return_value=72) as download, \
             patch.object(speedtest, "measure_upload", return_value=15) as upload:
            result = speedtest.run_speedtest("Tenda", iface="wlx123")
        self.assertEqual(result.server, speedtest.LIBRESPEED.name)
        self.assertEqual([c.kwargs["server"] for c in ping.call_args_list], list(speedtest.SERVERS))
        for transfer in (download, upload):
            transfer.assert_called_once()
            self.assertEqual(transfer.call_args.kwargs["server"], speedtest.LIBRESPEED)
            self.assertEqual(transfer.call_args.kwargs["iface"], "wlx123")

    def test_error_de_subida_no_repite_la_descarga(self):
        with patch.object(speedtest, "measure_ping", return_value=15), \
             patch.object(speedtest, "measure_download", return_value=80) as download, \
             patch.object(speedtest, "measure_upload", side_effect=speedtest.ServerError("HTTP 429")):
            with self.assertRaisesRegex(RuntimeError, "subida con Cloudflare.*HTTP 429"):
                speedtest.run_speedtest()
            download.assert_called_once()

    def test_servidor_manual_respeta_la_eleccion_en_caso_de_error(self):
        with patch.object(speedtest, "measure_ping", side_effect=speedtest.ServerError("DNS")) as ping:
            with self.assertRaisesRegex(RuntimeError, "latencia.*LibreSpeed.*DNS"):
                speedtest.run_speedtest(server=speedtest.LIBRESPEED)
            ping.assert_called_once()

    def test_cancelar_detiene_el_proceso_de_subida(self):
        cancel = threading.Event()
        proc = Mock()

        def interrupt(timeout=None):
            if timeout is not None:
                cancel.set()
                raise subprocess.TimeoutExpired("curl", timeout)
            return "", ""

        proc.communicate.side_effect = interrupt
        proc.poll.return_value = None
        with patch.object(speedtest.subprocess, "Popen") as popen:
            popen.return_value.__enter__.return_value = proc
            with self.assertRaises(InterruptedError):
                speedtest.measure_upload(cancel)
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)

    def test_ruta_automatica_usa_la_direccion_local_de_la_transferencia(self):
        devices = [{"ifname": "tun0", "addr_info": [{"local": "192.0.2.5"}]},
                   {"ifname": "wlan0", "addr_info": [{"local": "192.0.2.6"}]}]
        with patch.object(speedtest.subprocess, "check_output", return_value=json.dumps(devices)):
            self.assertEqual(speedtest._system_route({"192.0.2.5"}), "tun0")
            self.assertEqual(speedtest._system_route({"192.0.2.7"}), speedtest.SYSTEM_ROUTE)

    def test_descarga_incompleta_no_produce_velocidad(self):
        with patch.object(speedtest, "_transfer", return_value=speedtest.Transfer(1, 0, 0, "192.0.2.5")):
            with self.assertRaisesRegex(RuntimeError, "incompleta"):
                speedtest.measure_download()


if __name__ == "__main__":
    unittest.main()
