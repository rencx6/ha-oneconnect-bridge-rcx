from __future__ import annotations

DOMAIN = "oneconnect_bridge"

CONF_INTERFACE_IP = "interface_ip"
CONF_MAC = "mac"
CONF_SERIAL = "serial"

VIRTUAL_HARDWARE_VERSION = "1.0.0"

DEFAULT_PRODUCT_ID = "ZG37"
DEFAULT_DEVICE_TYPE = "A17"
# Huawei cloud manufacturer ID used in the wire-level devInfo payload.
DEFAULT_MANUFACTURER = "002"
# Stable Python gateway identity; do not derive display metadata from a profile.
DEFAULT_MODEL = "HA-SCG-ZG37"
DEFAULT_PROTOCOL_TYPE = 1

GATEWAY_MANUFACTURER = "华为"

DISCOVERY_PORT = 5683
DISCOVERY_MULTICAST = "238.238.238.238"
PIN_TTL_SECONDS = 300

# HA entity domains selectable in the "add entities" options flow.
# 全部支持开关包装（turn_on/turn_off）；传感器类只读域见 README 路线图。
BRIDGEABLE_DOMAINS = (
    "switch",
    "light",
    "input_boolean",
    "input_button",
    "script",
    "scene",
    "lock",
    "button",
    "automation",
    "cover",
    "fan",
    "humidifier",
    "media_player",
    "siren",
    "valve",
    "water_heater",
)

# Keep local/cloud abuse-prevention limits fixed.
MAX_GATEWAY_INSTANCES = 3
MAX_CHILD_DEVICES_PER_GATEWAY = 64
