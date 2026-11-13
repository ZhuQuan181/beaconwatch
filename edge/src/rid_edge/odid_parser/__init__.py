"""Open Drone ID parser package.

Submodules implement individual message decoders; `message_pack` reassembles
multiple ODID messages that arrive inside a single 802.11 frame or BLE
advertisement. `__init__.py` exposes the high-level `parse_wifi_frame` and
`parse_ble_advertisement` entry points.
"""
