import os
import time
import requests
import pyudev

MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://moonraker:7125")
USB_IDS = os.environ.get("USB_IDS", "1a86:7523,2341:0042").split(",")  # Example: CH340 and Arduino

def firmware_restart():
    try:
        requests.post(f"{MOONRAKER_URL}/printer/firmware_restart")
        print("Sent firmware_restart to Moonraker.")
    except Exception as e:
        print(f"Error sending firmware_restart: {e}")

def printer_connected():
    context = pyudev.Context()
    found = []
    for device in context.list_devices(subsystem='usb'):
        vid = device.get('ID_VENDOR_ID')
        pid = device.get('ID_MODEL_ID')
        if vid and pid:
            id_str = f"{vid}:{pid}"
            if id_str in USB_IDS:
                found.append(id_str)
    return found

if __name__ == "__main__":
    was_connected = set()
    while True:
        connected = set(printer_connected())
        if connected and connected != was_connected:
            firmware_restart()
        was_connected = connected
        time.sleep(5)