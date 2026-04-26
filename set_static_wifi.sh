#!/bin/bash
# Set a static IP for a NetworkManager-managed WiFi connection on Linux
# Usage: sudo ./set_static_wifi.sh <connection_name> <ip_address> <gateway> <dns>
# Example: sudo ./set_static_wifi.sh "MIWIFI_7A19" 192.168.1.225/24 192.168.1.1 "192.168.1.1 8.8.8.8"

set -euo pipefail  # Stop on error, treat unset variables as errors, propagate pipe failures

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo)"
  exit 1
fi

if [ $# -lt 4 ]; then
  echo "Usage: sudo $0 <connection_name> <ip_address> <gateway> <dns>"
  exit 1
fi

CONN_NAME="$1"
IP="$2"
GATEWAY="$3"
DNS="$4"

nmcli con mod "$CONN_NAME" ipv4.addresses "$IP"
nmcli con mod "$CONN_NAME" ipv4.gateway "$GATEWAY"
nmcli con mod "$CONN_NAME" ipv4.dns "$DNS"
nmcli con mod "$CONN_NAME" ipv4.method manual
nmcli con down "$CONN_NAME"
nmcli con up "$CONN_NAME"
