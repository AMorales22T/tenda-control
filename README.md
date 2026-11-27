# tenda-control

Utilidad de escritorio (GTK 4 + libadwaita + libnm) para gestionar un adaptador USB Wi-Fi/Bluetooth **Tenda AIC 8800D80** junto con las radios internas del equipo en Linux.

## Funciones

- Estado en tiempo real del adaptador Tenda, la tarjeta interna, Bluetooth y la salida IPv4 activa.
- Selector de redes Wi-Fi para el Tenda integrado con NetworkManager (incluye soporte para redes WPA3/WPA2/abiertas y perfiles empresariales 802.1X como `eduroam` con multiconexión y selección de AP compatible por banda).
- Modos de distribución de radios y control manual mediante `pkexec`.
- Regla `udev` persistente para elegir qué radios del USB se habilitan al conectarlo.
- Prueba de velocidad manual por interfaz o por la ruta del sistema.

## Requisitos

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
