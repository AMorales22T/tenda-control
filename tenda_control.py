#!/usr/bin/env python3
"""Utilidad de escritorio para el adaptador USB Tenda AIC 8800D80."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from tenda_hardware import SystemRadioState, WifiInfo, get_system_state
from tenda_networks import NetworkPicker
from tenda_helper import PROFILES
from tenda_speedtest import SERVERS, SYSTEM_ROUTE, SpeedTestResult, evaluate_speed, run_speedtest

APP_ID = "com.dolfius.TendaControl"
BASE_DIR = Path(__file__).resolve().parent
PROFILE_NAMES = {
    "dividido": ("Wi-Fi por Tenda", "Bluetooth por el equipo"),
    "invertido": ("Wi-Fi por el equipo", "Bluetooth por el Tenda"),
    "solo_interna": ("Solo equipo", "Wi-Fi y Bluetooth internos"),
    "todo": ("Todos los adaptadores", "Wi-Fi y Bluetooth en ambos dispositivos"),
}
USB_MODES = {
    "dividido": "Solo Wi-Fi", "invertido": "Solo Bluetooth",
    "solo_interna": "Ambas radios apagadas", "todo": "Wi-Fi y Bluetooth",
}
RADIO_NAMES = {
    "tenda_wifi": "Wi-Fi del Tenda", "tenda_bt": "Bluetooth del Tenda",
    "internal_wifi": "Wi-Fi del equipo", "internal_bt": "Bluetooth del equipo",
}

CSS = b"""
.brand { color: #cd5015; font-size: 27px; font-weight: 800; letter-spacing: -1.3px; }
.hero { padding: 19px; border-radius: 12px; background-color: #fff1e6; color: #442916; }
.hero .model { color: #72462c; }
.primary { background-color: #b44810; color: white; }
.primary:hover { background-color: #983b0c; }
"""


def wifi_detail(radio: WifiInfo) -> str:
    if not radio.exists:
        return "No detectado"
    if not radio.enabled:
        return "Desactivado"
    if radio.ssid or radio.state.startswith("100 "):
        strength = f"Señal {radio.signal} %" if radio.signal is not None else ""
        return " · ".join(filter(None, (radio.ssid or "Conectado", radio.freq_band, strength, radio.iface)))
    if not radio.iface:
        return "Autorizado · sin interfaz de red"
    if radio.state in ("up", "down", "unknown", "dormant", "lowerlayerdown"):
        return f"{radio.iface} · estado de red sin confirmar"
    return f"{radio.iface} · sin conexión Wi-Fi"


def row_with_icon(group, title, icon):
    row = Adw.ActionRow(title=title, use_markup=False)
    row.add_prefix(Gtk.Image.new_from_icon_name(icon))
    group.add(row)
    return row


class TendaControlWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Control Tenda")
        self.set_default_size(720, 685)
        self.state: SystemRadioState | None = None
        self.busy = False
        self.reading = False
        self.closed = False
        self.updating = False
        self.generation = 0
        self.last_default = None
        self.speed_running = False
        self.speed_cancel = threading.Event()

        self.overlay = Adw.ToastOverlay()
        self.set_content(self.overlay)
        layout = Adw.ToolbarView()
        self.overlay.set_child(layout)
        header = Adw.HeaderBar()
        brand = Gtk.Label(label="Tenda")
        brand.add_css_class("brand")
        header.pack_start(brand)
        header.set_title_widget(Gtk.Label(label="Control de adaptadores"))
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Actualizar estado")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        self.spinner = Gtk.Spinner()
        header.pack_end(self.refresh_btn)
        header.pack_end(self.spinner)
        layout.add_top_bar(header)

        self.views = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.views, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        layout.add_top_bar(switcher)
        layout.set_content(self.views)

        overview = self.page("estado", "Estado", "network-wireless-symbolic")
        hero = Gtk.Box(spacing=14)
        hero.add_css_class("hero")
        icon = Gtk.Image.new_from_file(str(BASE_DIR / "tenda-control.svg"))
        icon.set_pixel_size(52)
        hero.append(icon)
        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER)
        hero.append(identity)
        self.usb_title = Gtk.Label(label="Buscando adaptador…", xalign=0, wrap=True)
        self.usb_title.add_css_class("title-2")
        identity.append(self.usb_title)
        model = Gtk.Label(label="Adaptador USB  ·  AIC 8800D80", xalign=0)
        model.add_css_class("model")
        identity.append(model)
        overview.append(hero)

        networks = Adw.PreferencesGroup(title="Conexión Wi-Fi")
        overview.append(networks)
        self.wifi_rows = {
            key: row_with_icon(networks, title, "network-wireless-symbolic")
            for key, title in (("tenda_wifi", "Adaptador Tenda"), ("internal_wifi", "Tarjeta interna"))
        }
        self.route_row = row_with_icon(networks, "Salida a internet (IPv4)", "network-transmit-receive-symbolic")
        self.route_row.set_tooltip_text("Ruta que elegiría el sistema hacia 1.1.1.1.")
        self.network_btn = Gtk.Button(label="Elegir red…", valign=Gtk.Align.CENTER)
        self.network_btn.connect("clicked", self.choose_network)
        self.wifi_rows["tenda_wifi"].add_suffix(self.network_btn)
        bluetooth = Adw.PreferencesGroup(title="Bluetooth")
        overview.append(bluetooth)
        self.bt_row = row_with_icon(bluetooth, "Adaptadores habilitados", "bluetooth-symbolic")

        self.reset_btn = Gtk.Button(label="Reiniciar el USB…", halign=Gtk.Align.START)
        self.reset_btn.connect("clicked", self.confirm_reset)
        overview.append(self.reset_btn)

        controls = self.page("controles", "Controles", "preferences-system-symbolic")
        modes = Adw.PreferencesGroup(title="Modo de uso")
        controls.append(modes)
        self.mode = Adw.ComboRow(title="Distribución de las radios", model=Gtk.StringList.new(
            [pair[0] for pair in PROFILE_NAMES.values()]))
        modes.add(self.mode)
        self.apply_btn = Gtk.Button(label="Aplicar modo", valign=Gtk.Align.CENTER)
        self.apply_btn.add_css_class("primary")
        apply_row = Adw.ActionRow(use_markup=False)
        modes.add(apply_row)
        self.mode_detail = apply_row
        apply_row.add_suffix(self.apply_btn)
        self.mode.connect("notify::selected", lambda *_: self.sync_controls())
        self.apply_btn.connect("clicked", self.apply_mode)

        manual = Adw.PreferencesGroup(title="Control manual")
        controls.append(manual)
        self.switches = {}
        for key, label in RADIO_NAMES.items():
            row = Adw.SwitchRow(title=label, use_markup=False)
            manual.add(row)
            self.switches[key] = row
            row.connect("notify::active", self.toggle, key)

        startup = self.page("usb", "Al conectar", "drive-harddisk-usb-symbolic")
        defaults = Adw.PreferencesGroup(title="Al enchufar el Tenda",
            description="Elige qué radios del USB se habilitan al conectarlo, incluso con la aplicación cerrada.")
        startup.append(defaults)
        self.saved_row = row_with_icon(defaults, "Configuración guardada", "document-properties-symbolic")
        self.default_mode = Adw.ComboRow(title="Radios del USB", model=Gtk.StringList.new(list(USB_MODES.values())))
        defaults.add(self.default_mode)
        self.service_note = Gtk.Label(xalign=0, wrap=True, visible=False, label=
            "En este equipo, Solo Wi-Fi conserva la regla que solicita el cambio automático de la tarjeta interna.")
        startup.append(self.service_note)
        self.save_btn = Gtk.Button(label="Guardar configuración", halign=Gtk.Align.START)
        self.save_btn.add_css_class("primary")
        startup.append(self.save_btn)
        self.save_btn.connect("clicked", self.save_default)
        tests = self.page("velocidad", "Velocidad", "network-transmit-receive-symbolic")
        speed = Adw.PreferencesGroup(title="Prueba de velocidad",
            description="Medición manual. Descarga hasta 5 MB y sube 1 MB por prueba.")
        tests.append(speed)
        self.speed_adapter = Adw.ComboRow(title="Conexión que se medirá", model=Gtk.StringList.new(
            [SYSTEM_ROUTE, "Wi-Fi del Tenda", "Wi-Fi del equipo"]))
        speed.add(self.speed_adapter)
        self.speed_server = Adw.ComboRow(title="Servidor de prueba", model=Gtk.StringList.new(
            ["Automático", *(server.name for server in SERVERS)]))
        speed.add(self.speed_server)
        self.speed_row = Adw.ActionRow(title="Medición", use_markup=False)
        speed.add(self.speed_row)
        self.speed_btn = Gtk.Button(label="Iniciar prueba", valign=Gtk.Align.CENTER)
        self.speed_btn.add_css_class("primary")
        self.speed_btn.connect("clicked", self.toggle_speedtest)
        self.speed_row.add_suffix(self.speed_btn)
        self.speed_adapter.connect("notify::selected", self.speed_selection_changed)
        self.speed_history = Adw.PreferencesGroup(title="Últimas mediciones de esta sesión", visible=False)
        tests.append(self.speed_history)
        self.history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.history_list.add_css_class("boxed-list")
        self.speed_history.add(self.history_list)
        self.speed_row.set_subtitle("Lista para iniciar")
        self.sync_controls()
        self.timer = GLib.timeout_add_seconds(4, self.refresh)
        self.connect("close-request", self.on_close)
        self.refresh()

    def page(self, key, title, icon):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.views.add_titled_with_icon(scroll, key, title, icon)
        clamp = Adw.Clamp(maximum_size=650)
        scroll.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
                      margin_start=18, margin_end=18, margin_top=22, margin_bottom=24)
        clamp.set_child(box)
        return box

    def on_close(self, *_):
        self.closed = True
        self.speed_cancel.set()
        GLib.source_remove(self.timer)
        return False

    def speed_target(self):
        choice = self.speed_adapter.get_selected()
        if choice == 0:
            return SYSTEM_ROUTE, None
        key = ("tenda_wifi", "internal_wifi")[choice - 1]
        radio = getattr(self.state, key, None)
        iface = radio.iface if radio and radio.enabled else None
        return RADIO_NAMES[key], iface

    def speed_selection_changed(self, *_):
        self.speed_row.set_subtitle("Lista para iniciar")
        self.sync_controls()

    def toggle_speedtest(self, *_):
        if self.speed_running:
            self.speed_cancel.set()
            self.speed_btn.set_sensitive(False)
            self.speed_row.set_subtitle("Cancelando prueba…")
            return
        label, iface = self.speed_target()
        if self.busy or (label != SYSTEM_ROUTE and not iface):
            return
        via = f"{label} ({iface})" if iface else label
        choice = self.speed_server.get_selected()
        server = SERVERS[choice - 1] if choice else None
        self.speed_running = True
        self.speed_cancel.clear()
        self.speed_btn.set_label("Cancelar")
        self.speed_btn.remove_css_class("primary")
        self.sync_controls()

        def show_progress(message):
            if not self.closed and not self.speed_cancel.is_set():
                self.speed_row.set_subtitle(message)
            return False

        def on_progress(message):
            GLib.idle_add(show_progress, message)

        def worker():
            try:
                result = run_speedtest(via, on_progress, self.speed_cancel, iface=iface, server=server)
                GLib.idle_add(done, result, "")
            except Exception as exc:
                GLib.idle_add(done, None, str(exc))

        def done(result, error):
            if self.closed:
                return False
            self.speed_running = False
            self.speed_btn.set_label("Iniciar prueba")
            self.speed_btn.add_css_class("primary")
            if self.speed_cancel.is_set():
                self.speed_row.set_subtitle("Prueba cancelada")
            elif result is None:
                self.speed_row.set_subtitle(error or "No se pudo completar la prueba")
            else:
                self.add_speed_result(result)
                self.speed_row.set_subtitle("Prueba completada")
            self.sync_controls()
            return False

        # Al cerrar la ventana, el proceso de medición debe terminar antes de salir.
        threading.Thread(target=worker, daemon=False).start()

    def add_speed_result(self, result: SpeedTestResult):
        stamp = datetime.now().strftime("%H:%M:%S")
        _, verdict = evaluate_speed(result)
        metrics = (f"Descarga {result.download_mbps:.1f} Mbps   ·   Subida {result.upload_mbps:.1f} Mbps"
                   f"\nLatencia HTTPS {result.ping_ms:.0f} ms").replace(".", ",")
        row = Adw.ActionRow(title=f"{stamp}   ·   {result.via}   ·   {result.server}", subtitle=metrics, use_markup=False)
        row.set_tooltip_text(f"Valoración orientativa de esta medición: {verdict}")
        self.history_list.prepend(row)
        oldest = self.history_list.get_row_at_index(5)
        if oldest is not None:
            self.history_list.remove(oldest)
        self.speed_history.set_visible(True)

    def choose_network(self, *_):
        if self.state and self.state.tenda_wifi.enabled and self.state.tenda_wifi.iface:
            NetworkPicker(self, self.state.tenda_wifi.iface).present()

    def confirm_reset(self, *_):
        dialog = Adw.MessageDialog(transient_for=self, heading="¿Reiniciar el adaptador Tenda?",
            body="Las conexiones Wi-Fi y Bluetooth que usen el USB se interrumpirán durante el reinicio.")
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("reset", "Reiniciar")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.connect("response", lambda _, response: response == "reset" and
                       self.run_helper(["reset-tenda"], "Adaptador reiniciado"))
        dialog.present()

    def toggle(self, row, _pspec, key):
        if not self.updating and not self.busy:
            self.run_helper(["set", key, "on" if row.get_active() else "off"], "Control actualizado")

    def apply_mode(self, *_):
        key = list(PROFILE_NAMES)[self.mode.get_selected()]
        self.run_helper(["profile", key], f"Modo aplicado · {PROFILE_NAMES[key][0]}")

    def save_default(self, *_):
        key = list(USB_MODES)[self.default_mode.get_selected()]
        self.run_helper(["save-default", key], "Configuración guardada para la próxima conexión")

    def sync_controls(self):
        ready = self.state is not None and not self.busy and not self.speed_running
        self.refresh_btn.set_sensitive(not self.busy and not self.reading)
        self.reset_btn.set_sensitive(bool(ready and self.state.tenda_connected))
        self.network_btn.set_sensitive(bool(ready and self.state.tenda_wifi.enabled and self.state.tenda_wifi.iface))
        key = list(PROFILE_NAMES)[self.mode.get_selected()]
        can_apply = ready and all(getattr(self.state, k).exists for k, v in PROFILES[key].items() if v)
        self.apply_btn.set_sensitive(bool(can_apply))
        self.mode_detail.set_title(PROFILE_NAMES[key][1])
        self.mode_detail.set_subtitle("Falta un adaptador para este modo" if ready and not can_apply else "")
        self.mode.set_sensitive(ready)
        self.save_btn.set_sensitive(ready)
        self.default_mode.set_sensitive(ready)
        for target, row in self.switches.items():
            row.set_sensitive(bool(ready and getattr(self.state, target).exists))
        self.spinner.set_spinning(self.busy or self.reading or self.speed_running)
        label, iface = self.speed_target()
        available = label == SYSTEM_ROUTE or bool(iface)
        self.speed_server.set_sensitive(not self.busy and not self.speed_running)
        self.speed_adapter.set_sensitive(not self.busy and not self.speed_running)
        if not self.speed_running:
            self.speed_btn.set_sensitive(not self.busy and available)
            self.speed_adapter.set_subtitle(iface or ("Interfaz elegida por el sistema" if available else "Adaptador no disponible"))

    def run_helper(self, args, success):
        if self.busy or self.closed or self.speed_running:
            return
        self.busy = True
        self.generation += 1
        self.sync_controls()

        def worker():
            try:
                result = subprocess.run(["pkexec", "/usr/bin/python3", "-Es", str(BASE_DIR / "tenda_helper.py"), *args],
                                        capture_output=True, text=True, timeout=120)
                message = {0: success, 126: "Operación cancelada.", 127: "No se obtuvo autorización."}.get(
                    result.returncode, result.stderr.strip() or "No se pudo aplicar el cambio.")
            except (OSError, subprocess.SubprocessError) as exc:
                message = f"No se pudo completar la operación. {exc}"
            GLib.idle_add(done, message)

        def done(message):
            if not self.closed:
                self.busy = False
                self.overlay.add_toast(Adw.Toast.new(message))
                self.refresh()
            return False

        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        if self.busy or self.reading or self.closed:
            return not self.closed
        generation = self.generation
        self.reading = True
        self.sync_controls()

        def worker():
            try:
                state = get_system_state()
                error = ""
            except Exception as exc:
                state, error = None, str(exc)
            GLib.idle_add(done, state, error)

        def done(state, error):
            self.reading = False
            if self.closed:
                return False
            if generation != self.generation:
                self.refresh()
                return False
            self.state = state
            if state:
                self.render_state(state)
            else:
                self.usb_title.set_text("Estado no disponible")
                for row in (*self.wifi_rows.values(), self.route_row, self.bt_row, self.saved_row):
                    row.set_subtitle("Sin datos")
                self.overlay.add_toast(Adw.Toast.new(f"Error al leer los adaptadores. {error}"))
            self.sync_controls()
            return False

        threading.Thread(target=worker, daemon=True).start()
        return True

    def render_state(self, state):
        self.usb_title.set_text("Tenda conectado" if state.tenda_connected else "Tenda desconectado")
        self.usb_title.set_tooltip_text(state.tenda_usb_path)
        for key, row in self.wifi_rows.items():
            row.set_subtitle(wifi_detail(getattr(state, key)))
        iface = state.outbound_iface
        route = iface or "Ruta no disponible"
        if iface and iface == state.tenda_wifi.iface:
            route = f"Tenda ({iface})"
        elif iface and iface == state.internal_wifi.iface:
            route = f"Wi-Fi del equipo ({iface})"
        self.route_row.set_subtitle(route)
        enabled = []
        for radio, name in ((state.tenda_bt, "Tenda"), (state.internal_bt, "Equipo")):
            if radio.enabled:
                enabled.append(f"{name} ({radio.hci})" if radio.hci else f"{name} · sin controlador HCI")
        self.bt_row.set_subtitle(" · ".join(enabled) or "Ninguno habilitado")
        saved = state.default_profile
        self.saved_row.set_subtitle(USB_MODES.get(saved, "Sin regla guardada" if saved == "ninguno" else "Regla personalizada"))
        self.service_note.set_visible(state.has_wifi_service_rule)
        self.save_btn.set_tooltip_text("Sustituye la regla del Tenda y conserva una copia de la original.")
        if saved != self.last_default and saved in USB_MODES:
            self.default_mode.set_selected(list(USB_MODES).index(saved))
        self.last_default = saved
        self.updating = True
        try:
            for key, row in self.switches.items():
                radio = getattr(state, key)
                row.set_active(radio.enabled)
                row.set_subtitle((radio.iface if isinstance(radio, WifiInfo) else radio.hci)
                                 or ("Disponible" if radio.exists else "No detectado"))
        finally:
            self.updating = False


class TendaControlApp(Adw.Application):
    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("Control Tenda")
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        window = self.props.active_window or TendaControlWindow(self)
        window.present()


if __name__ == "__main__":
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        raise SystemExit("Abre Control Tenda desde una sesión gráfica de escritorio.")
    raise SystemExit(TendaControlApp(application_id=APP_ID).run(sys.argv))
