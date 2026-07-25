#!/usr/bin/env python3
import argparse
import os
import time

import serial


def open_serial(port: str, baud: int) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.5
    # Set modem-control lines before opening; changing them after open can
    # briefly assert the ESP32-S3 USB-Serial/JTAG reset/download sequence.
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main() -> int:
    parser = argparse.ArgumentParser(description="Read StackChan serial logs without forcing a reset.")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    end = time.time() + args.seconds
    chunks: list[bytes] = []
    ser: serial.Serial | None = None
    opened = 0
    while time.time() < end:
        if ser is None:
            if not os.path.exists(args.port):
                time.sleep(0.2)
                continue
            try:
                ser = open_serial(args.port, args.baud)
                opened += 1
                action = "opened" if opened == 1 else "reopened"
                print(
                    f"serial {action}: port={args.port}, baud={args.baud}, "
                    "dtr=false, rts=false"
                )
            except serial.SerialException:
                time.sleep(0.2)
                continue

        try:
            data = ser.read(4096)
        except serial.SerialException as exc:
            print(f"\nserial disconnected ({exc}); waiting for re-enumeration...")
            try:
                ser.close()
            except serial.SerialException:
                pass
            ser = None
            time.sleep(0.2)
            continue

        if data:
            chunks.append(data)
            print(data.decode("utf-8", "replace"), end="")

    if ser is not None:
        ser.close()

    output = b"".join(chunks).decode("utf-8", "replace")
    print("\n--- diagnosis ---")
    last_download = max(output.rfind("boot:0x23"), output.rfind("waiting for download"))
    last_firmware = max(
        output.rfind("SPI_FAST_FLASH_BOOT"),
        output.rfind("Project name:     stack-chan"),
        output.rfind("M5Stack-StackChan"),
        output.rfind("[HAL]"),
    )
    if last_firmware > last_download:
        print("Firmware appears to be booting. Watch the bridge logs for OTA/WebSocket requests.")
        return 0
    if last_download >= 0:
        print("Device is still in ESP32-S3 download mode.")
        print("Short-press and immediately release RST. Do not hold it until the green LED appears.")
        return 2
    if "StackChan" in output or "Application" in output or "wifi" in output.lower():
        print("Firmware appears to be booting. Watch the bridge logs for OTA/WebSocket requests.")
        return 0
    if not output.strip():
        print("No serial output observed. Press Reset once, or check cable/power.")
        return 1
    print("Serial output observed, but boot state is inconclusive.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
