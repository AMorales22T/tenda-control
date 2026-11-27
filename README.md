# tenda-control

Utilidad de escritorio comunitaria y no oficial (GTK 4 + libadwaita + libnm) para gestionar adaptadores USB Wi-Fi/Bluetooth **Tenda AIC 8800D80** (`2604:001f`) junto con las radios internas del equipo en Linux.

> **Aviso:** Proyecto independiente y de código abierto sin afiliación ni respaldo oficial de Tenda.

## Funciones

- Estado en tiempo real del adaptador USB Tenda, la tarjeta Wi-Fi interna, los controladores Bluetooth (detección genérica por clase USB `e0/01/01`) y la salida IPv4 activa.
- Selector de redes Wi-Fi para el Tenda integrado con NetworkManager (WPA3, WPA2 personal, abiertas y redes empresariales 802.1X como `eduroam` con soporte para perfiles multiconexión y selección de AP compatible por banda).
- Modos preconfigurados de distribución de radios y conmutación manual mediante `pkexec`.
- Regla `udev` persistente (`/etc/udev/rules.d/99-tenda-wifi.rules`) para decidir qué radios del USB se habilitan al conectarlo.
- Prueba de velocidad manual por interfaz o por la ruta del sistema.

## Requisitos

- Adaptador USB Tenda AIC 8800D80 (`2604:001f`) con el driver `aic8800` instalado.
- Python 3 con PyGObject (`GTK 4`, `libadwaita >= 1.4`, `libnm`).
- `pkexec`, `nmcli`, `ip`, `udevadm` y `curl`.

## Instalación

```bash
./install.sh
```

## Pruebas

```bash
python3 -m unittest -v test_tenda.py test_tenda_networks.py
```

## Licencia

Distribuido bajo licencia [MIT](LICENSE).
