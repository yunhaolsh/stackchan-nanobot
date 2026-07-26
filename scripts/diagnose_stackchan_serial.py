#!/usr/bin/env python3
import argparse
import errno
import os
import select
import time


class SerialDisconnected(OSError):
    pass


class ReadOnlySerial:
    """Read a Linux CDC ACM endpoint without touching modem-control lines."""

    def __init__(self, port: str, timeout: float = 0.5):
        self.port = port
        self.timeout = timeout
        self.fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)

    def read(self, size: int) -> bytes:
        try:
            readable, _, _ = select.select([self.fd], [], [], self.timeout)
            if not readable:
                return b""
            data = os.read(self.fd, size)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.ENODEV, errno.ENXIO, errno.EBADF):
                raise SerialDisconnected(str(exc)) from exc
            raise
        if not data:
            raise SerialDisconnected("serial endpoint returned EOF")
        return data

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def open_serial(port: str, baud: int) -> ReadOnlySerial:
    # USB Serial/JTAG ignores the UART baud rate. Keep the argument in the CLI
    # for compatibility, but never issue DTR/RTS ioctls from this monitor.
    del baud
    return ReadOnlySerial(port)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read StackChan serial logs without forcing a reset.")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    end = time.time() + args.seconds
    chunks: list[bytes] = []
    ser: ReadOnlySerial | None = None
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
                    f"serial {action}: port={args.port}, backend=readonly, "
                    "modem-control=untouched"
                )
            except OSError:
                time.sleep(0.2)
                continue

        try:
            data = ser.read(4096)
        except SerialDisconnected as exc:
            print(f"\nserial disconnected ({exc}); waiting for re-enumeration...")
            try:
                ser.close()
            except OSError:
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
