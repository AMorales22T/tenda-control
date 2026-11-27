"""Selección de redes del USB mediante el cliente nativo de NetworkManager."""
import uuid

import gi

gi.require_version("NM", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, NM

from tenda_hardware import TENDA_PID, TENDA_VID, find_usb_device, find_wifi_iface_for_usb

SEC = getattr(NM, "80211ApSecurityFlags")
PRIVACY = getattr(NM, "80211ApFlags").PRIVACY
MULTI_CONNECT = (NM.ConnectionMultiConnect.MANUAL_MULTIPLE, NM.ConnectionMultiConnect.MULTIPLE)


def security(ap):
    flags = ap.get_rsn_flags() | ap.get_wpa_flags()
    if flags & SEC.KEY_MGMT_SAE:
        return "sae", "WPA3"
    if flags & SEC.KEY_MGMT_PSK:
        return "wpa-psk", "WPA personal"
    if flags & (SEC.KEY_MGMT_OWE | SEC.KEY_MGMT_OWE_TM):
        return "owe", "Abierta cifrada"
    if flags & SEC.KEY_MGMT_802_1X:
        return None, "WPA empresarial"
    if flags or ap.get_flags() & PRIVACY:
        return None, "Requiere perfil configurado"
    return "", "Abierta"


def network_key(ap):
    return ap.get_ssid().get_data(), security(ap)[0]


def candidates(device):
    """Un punto por red y seguridad. Prioriza el actual y después la señal."""
    current = device.get_active_access_point()
    aps = sorted(device.get_access_points(),
                 key=lambda ap: (ap == current, ap.get_strength()), reverse=True)
    networks = {}
    for ap in aps:
        if ap.get_ssid() and ap.get_ssid().get_size():
            networks.setdefault(network_key(ap), ap)
    return list(networks.values())


def allows_multiple(profile):
    conn = profile.get_setting_connection()
    return bool(conn and conn.get_multi_connect() in MULTI_CONNECT)


def saved_match(client, device, ap):
    # Activar un perfil de uso único en otro dispositivo puede desconectarlo.
    occupied = {a.get_uuid() for a in client.get_active_connections()
                if any(d != device for d in a.get_devices())}
    profiles = [p for p in device.get_available_connections()
                if p.get_uuid() not in occupied or allows_multiple(p)]
    if not profiles:
        return None, ap
    key = network_key(ap)
    # Si el perfil restringe banda (p. ej. 5 GHz), busca otro AP compatible de la misma red.
    same_net = [a for a in device.get_access_points()
                if a != ap and a.get_ssid() and a.get_ssid().get_size() and network_key(a) == key]
    same_net.sort(key=lambda a: a.get_strength(), reverse=True)
    for candidate in (ap, *same_net):
        for profile in profiles:
            if candidate.connection_valid(profile):
                return profile, candidate
    return None, ap


def saved_profile(client, device, ap):
    return saved_match(client, device, ap)[0]


def new_profile(iface, ap, password):
    method, _ = security(ap)
    if method is None:
        raise ValueError("Configura primero las credenciales de esta red en NetworkManager.")
    if method == "wpa-psk" and not NM.utils_wpa_psk_valid(password):
        raise ValueError("Introduce una contraseña WPA de 8 a 63 caracteres o una clave hexadecimal de 64 dígitos.")
    if method == "sae" and not password:
        raise ValueError("Introduce la contraseña de la red.")
    profile = NM.SimpleConnection.new()
    name = NM.utils_ssid_to_utf8(ap.get_ssid().get_data())
    base = NM.SettingConnection(id=f"Tenda · {name}", uuid=str(uuid.uuid4()),
                                type="802-11-wireless", interface_name=iface)
    base.add_permission("user", GLib.get_user_name(), None)
    profile.add_setting(base)
    profile.add_setting(NM.SettingWireless(ssid=ap.get_ssid(), mode="infrastructure"))
    if method:
        settings = NM.SettingWirelessSecurity(key_mgmt=method)
        if method in ("sae", "wpa-psk"):
            settings.props.psk = password
        profile.add_setting(settings)
    return profile


def require_tenda(iface):
    usb = find_usb_device(TENDA_VID, TENDA_PID)
    if not iface or find_wifi_iface_for_usb(usb) != iface:
        raise ValueError("El Tenda ya no está disponible. Vuelve a abrir el selector.")


class NetworkPicker(Adw.Window):
    def __init__(self, parent, iface):
        super().__init__(transient_for=parent, modal=True, title="Red Wi-Fi del Tenda",
                         default_width=450, destroy_with_parent=True)
        self.iface = iface
        self.client = self.device = self.active = None
        self.aps = []
        self.closed = self.connecting = False
        self.cancel = Gio.Cancellable()
        self.timer = 0
        self.scan_handler = 0
        view = Adw.ToolbarView()
        self.set_content(view)
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=14,
                      margin_start=18, margin_end=18, margin_bottom=18)
        view.set_content(box)
        group = Adw.PreferencesGroup(title=f"Adaptador Tenda ({iface})")
        box.append(group)
        self.choice = Adw.ComboRow(title="Red disponible", use_markup=False)
        group.add(self.choice)
        self.password = Adw.PasswordEntryRow(title="Contraseña")
        group.add(self.password)
        self.password.set_visible(False)
        self.status = Gtk.Label(label="Consultando redes…", xalign=0, wrap=True)
        box.append(self.status)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        box.append(actions)
        self.scan = Gtk.Button(label="Buscar redes", sensitive=False)
        self.join = Gtk.Button(label="Conectar", sensitive=False)
        self.join.add_css_class("primary")
        actions.append(self.scan)
        actions.append(self.join)
        self.choice_handler = self.choice.connect("notify::selected", self.select)
        self.scan.connect("clicked", self.rescan)
        self.join.connect("clicked", self.connect_selected)
        self.connect("close-request", self.closing)
        NM.Client.new_async(self.cancel, self.client_ready)

    def client_ready(self, _, result):
        if self.closed:
            return
        try:
            self.client = NM.Client.new_finish(result)
            require_tenda(self.iface)
            device = self.client.get_device_by_iface(self.iface)
            if not isinstance(device, NM.DeviceWifi) or not device.get_managed():
                raise ValueError("NetworkManager no gestiona el Wi-Fi del Tenda.")
            self.device = device
            self.scan_handler = device.connect("notify::last-scan", self.populate)
            self.populate()
            self.scan.set_sensitive(True)
        except (GLib.Error, ValueError) as exc:
            self.status.set_text(str(exc))

    def populate(self, *_):
        if self.connecting or self.closed:
            return
        previous = self.selected()
        self.aps = candidates(self.device)
        labels = [f"{NM.utils_ssid_to_utf8(ap.get_ssid().get_data())} · {ap.get_strength()} % · {security(ap)[1]}"
                  for ap in self.aps]
        with self.choice.handler_block(self.choice_handler):
            self.choice.set_model(Gtk.StringList.new(labels))
            if previous:
                for i, ap in enumerate(self.aps):
                    if network_key(ap) == network_key(previous):
                        self.choice.set_selected(i)
                        break
        self.select()

    def selected(self):
        index = self.choice.get_selected()
        return self.aps[index] if index < len(self.aps) else None

    def select(self, *_):
        ap = self.selected()
        key = network_key(ap) if ap else None
        if key != getattr(self, "password_for", None):
            self.password.set_text("")
        self.password_for = key
        current = self.device.get_active_access_point() if self.device else None
        connected = bool(current and current.get_ssid() and network_key(current) == key)
        profile = saved_profile(self.client, self.device, ap) if ap else None
        method = security(ap)[0] if ap else None
        available = bool(ap and (profile or method is not None))
        self.password.set_visible(bool(not connected and not profile and method in ("sae", "wpa-psk")))
        self.join.set_sensitive(available and not connected and not self.connecting)
        if not ap:
            message = "No hay redes visibles. Pulsa Buscar redes."
        elif connected:
            message = "El Tenda ya está conectado a esta red."
        elif not available:
            message = "Esta red necesita un perfil configurado en los ajustes Wi-Fi del sistema."
        else:
            message = "Al conectar, el Tenda cambiará a la red seleccionada."
        self.status.set_text(message)

    def rescan(self, *_):
        try:
            require_tenda(self.iface)
        except ValueError as exc:
            self.status.set_text(str(exc))
            return
        self.scan.set_sensitive(False)
        self.status.set_text("Buscando redes…")
        self.device.request_scan_async(self.cancel, self.scan_requested)

    def scan_requested(self, device, result):
        if self.closed:
            return
        try:
            device.request_scan_finish(result)
        except GLib.Error as exc:
            self.status.set_text(exc.message)
        self.scan.set_sensitive(not self.connecting)

    def connect_selected(self, *_):
        ap = self.selected()
        if self.connecting or not ap:
            return
        try:
            require_tenda(self.iface)
            if self.client.get_device_by_iface(self.iface) != self.device or ap not in self.device.get_access_points():
                raise ValueError("La red o el adaptador han cambiado. Actualiza la lista.")
            profile, target_ap = saved_match(self.client, self.device, ap)
            if profile:
                activate = self.client.activate_connection_async
                self.finish = self.client.activate_connection_finish
                ap = target_ap
            else:
                profile = new_profile(self.iface, ap, self.password.get_text())
                activate = self.client.add_and_activate_connection_async
                self.finish = self.client.add_and_activate_connection_finish
            self.target = network_key(ap)
            activate(profile, self.device, ap.get_path(), self.cancel, self.activation_started)
        except (ValueError, GLib.Error) as exc:
            self.status.set_text(str(exc))
            return
        self.connecting = True
        self.password.set_text("")
        for widget in (self.join, self.scan, self.password, self.choice):
            widget.set_sensitive(False)
        self.status.set_text("Conectando el Tenda…")

    def activation_started(self, _, result):
        if self.closed:
            return
        try:
            self.active = self.finish(result)
        except GLib.Error as exc:
            self.finished(exc.message)
            return
        self.deadline = GLib.get_monotonic_time() + 90 * GLib.TIME_SPAN_SECOND
        self.timer = GLib.timeout_add(400, self.verify)

    def verify(self):
        state = self.active.get_state()
        ap = self.device.get_active_access_point()
        if (state == NM.ActiveConnectionState.ACTIVATED and self.device.get_active_connection() == self.active
                and ap and ap.get_ssid() and network_key(ap) == self.target):
            message = f"Tenda conectado a {NM.utils_ssid_to_utf8(ap.get_ssid().get_data())}."
        elif state in (NM.ActiveConnectionState.DEACTIVATING, NM.ActiveConnectionState.DEACTIVATED):
            message = "No se pudo conectar el Tenda. Comprueba la contraseña y la señal."
        elif GLib.get_monotonic_time() >= self.deadline:
            message = "NetworkManager aún no ha confirmado la conexión."
        else:
            return True
        self.timer = 0
        self.finished(message)
        return False

    def finished(self, message):
        self.connecting = False
        self.active = None
        for widget in (self.scan, self.choice, self.password):
            widget.set_sensitive(True)
        self.populate()
        self.status.set_text(message)
        self.get_transient_for().refresh()

    def closing(self, *_):
        self.closed = True
        self.cancel.cancel()
        if self.scan_handler:
            self.device.handler_disconnect(self.scan_handler)
        if self.timer:
            GLib.source_remove(self.timer)
        return False
