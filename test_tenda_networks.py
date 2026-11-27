"""Redes simuladas y perfiles reales de libnm. Ninguna prueba activa una radio."""
import unittest
from unittest.mock import Mock, patch

import tenda_networks as wifi
from tenda_networks import GLib, NM, SEC, NetworkPicker


def access_point(ssid=b"Casa:5G", flags=SEC.KEY_MGMT_PSK, strength=72):
    ap = Mock()
    ap.get_ssid.return_value = GLib.Bytes.new(ssid)
    ap.get_flags.return_value = wifi.PRIVACY if flags else 0
    ap.get_rsn_flags.return_value = flags
    ap.get_wpa_flags.return_value = 0
    ap.get_strength.return_value = strength
    ap.get_path.return_value = "/org/freedesktop/NetworkManager/AccessPoint/19"
    return ap


def picker(ap):
    view = Mock()
    view.iface = "wlx123"
    view.connecting = False
    view.selected.return_value = ap
    view.client.get_device_by_iface.return_value = view.device
    view.client.get_active_connections.return_value = []
    view.device.get_available_connections.return_value = []
    view.device.get_access_points.return_value = [ap]
    view.password.get_text.return_value = "clave-simulada"
    return view


class NetworkTests(unittest.TestCase):
    def test_lista_conserva_red_actual_y_distingue_seguridad(self):
        same = access_point(strength=97)
        current = access_point(strength=30)
        open_ap = access_point(flags=0)
        hidden = access_point(b"")
        device = Mock()
        device.get_active_access_point.return_value = current
        device.get_access_points.return_value = [same, hidden, open_ap, current]
        self.assertEqual(wifi.candidates(device), [current, open_ap])

    def test_perfiles_de_red_validos_en_libnm(self):
        for flags, expected in ((SEC.KEY_MGMT_PSK, "wpa-psk"), (SEC.KEY_MGMT_SAE, "sae"),
                                (SEC.KEY_MGMT_OWE, "owe"), (0, None)):
            with self.subTest(flags=flags):
                profile = wifi.new_profile("wlx123", access_point(flags=flags), "clave-simulada")
                profile.normalize()
                self.assertTrue(profile.verify())
                self.assertTrue(profile.verify_secrets())
                self.assertEqual(profile.get_interface_name(), "wlx123")
                self.assertEqual(profile.get_setting_wireless().get_ssid().get_data(), b"Casa:5G")
                sec = profile.get_setting_wireless_security()
                self.assertEqual(sec.get_key_mgmt() if sec else None, expected)

    def test_clave_incorrecta_y_empresa_sin_perfil_no_activan(self):
        for flags in (SEC.KEY_MGMT_PSK, SEC.KEY_MGMT_802_1X):
            view = picker(access_point(flags=flags))
            view.password.get_text.return_value = "abc"
            with patch.object(wifi, "require_tenda"):
                NetworkPicker.connect_selected(view)
            view.client.add_and_activate_connection_async.assert_not_called()
            view.client.activate_connection_async.assert_not_called()

    def test_reutiliza_perfil_guardado_en_dispositivo_y_ap_elegidos(self):
        ap = access_point()
        view = picker(ap)
        saved = Mock()
        view.device.get_available_connections.return_value = [saved]
        with patch.object(wifi, "require_tenda"):
            NetworkPicker.connect_selected(view)
        view.client.activate_connection_async.assert_called_once_with(
            saved, view.device, ap.get_path(), view.cancel, view.activation_started)
        view.client.add_and_activate_connection_async.assert_not_called()

    def test_perfil_en_uso_en_interna_no_se_mueve_al_usb(self):
        ap = access_point()
        view = picker(ap)
        saved, active = Mock(), Mock()
        active.get_uuid.return_value = saved.get_uuid.return_value = "en-uso"
        active.get_devices.return_value = [Mock()]
        view.client.get_active_connections.return_value = [active]
        view.device.get_available_connections.return_value = [saved]
        with patch.object(wifi, "require_tenda"):
            NetworkPicker.connect_selected(view)
        profile, device, path, *_ = view.client.add_and_activate_connection_async.call_args.args
        self.assertIs(device, view.device)
        self.assertEqual(path, ap.get_path())
        self.assertEqual(profile.get_interface_name(), "wlx123")
        view.client.activate_connection_async.assert_not_called()

    def test_reutiliza_perfil_multiconexion_y_elige_ap_compatible(self):
        ap_24 = access_point(b"CorpWiFi", flags=SEC.KEY_MGMT_802_1X, strength=90)
        ap_5 = access_point(b"CorpWiFi", flags=SEC.KEY_MGMT_802_1X, strength=75)
        ap_5.get_path.return_value = "/org/freedesktop/NetworkManager/AccessPoint/55"
        ap_24.connection_valid.return_value = False
        ap_5.connection_valid.return_value = True
        view = picker(ap_24)
        view.device.get_access_points.return_value = [ap_24, ap_5]
        saved, active = Mock(), Mock()
        active.get_uuid.return_value = saved.get_uuid.return_value = "corp-uuid"
        active.get_devices.return_value = [Mock()]
        saved.get_setting_connection.return_value.get_multi_connect.return_value = (
            NM.ConnectionMultiConnect.MANUAL_MULTIPLE
        )
        view.client.get_active_connections.return_value = [active]
        view.device.get_available_connections.return_value = [saved]
        with patch.object(wifi, "require_tenda"):
            NetworkPicker.connect_selected(view)
        view.client.activate_connection_async.assert_called_once_with(
            saved, view.device, ap_5.get_path(), view.cancel, view.activation_started)
        view.client.add_and_activate_connection_async.assert_not_called()

    def test_usb_retirado_no_activa_nada(self):
        view = picker(access_point())
        with patch.object(wifi, "find_usb_device", return_value=None):
            NetworkPicker.connect_selected(view)
        view.client.activate_connection_async.assert_not_called()
        view.client.add_and_activate_connection_async.assert_not_called()

    def test_confirma_resultado_solo_en_red_y_dispositivo_correctos(self):
        target = access_point()
        view = picker(target)
        view.active.get_state.return_value = NM.ActiveConnectionState.ACTIVATED
        view.deadline = GLib.get_monotonic_time() + GLib.TIME_SPAN_SECOND
        view.target = wifi.network_key(target)
        view.device.get_active_connection.return_value = view.active
        view.device.get_active_access_point.return_value = access_point(b"Otra")
        self.assertTrue(NetworkPicker.verify(view))
        view.finished.assert_not_called()
        view.device.get_active_access_point.return_value = target
        self.assertFalse(NetworkPicker.verify(view))
        view.finished.assert_called_once_with("Tenda conectado a Casa:5G.")

    def test_cerrar_libera_observador_sin_desconectar_dispositivo(self):
        view = picker(access_point())
        view.timer = 0
        view.scan_handler = 47
        NetworkPicker.closing(view)
        view.device.handler_disconnect.assert_called_once_with(47)
        view.device.disconnect.assert_not_called()
