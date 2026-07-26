import errno
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "diagnose_stackchan_serial.py"
SPEC = importlib.util.spec_from_file_location("diagnose_stackchan_serial", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_readonly_serial_opens_without_modem_control(monkeypatch):
    opened = []
    monkeypatch.setattr(MODULE.os, "open", lambda port, flags: opened.append((port, flags)) or 7)
    serial = MODULE.ReadOnlySerial("/dev/ttyACM0")

    assert serial.fd == 7
    assert opened[0][0] == "/dev/ttyACM0"
    assert opened[0][1] & MODULE.os.O_NOCTTY
    assert opened[0][1] & MODULE.os.O_NONBLOCK


def test_readonly_serial_reports_usb_disconnect(monkeypatch):
    monkeypatch.setattr(MODULE.os, "open", lambda *_: 7)
    monkeypatch.setattr(MODULE.select, "select", lambda *_: ([7], [], []))

    def disconnected(*_):
        raise OSError(errno.EIO, "device disconnected")

    monkeypatch.setattr(MODULE.os, "read", disconnected)
    serial = MODULE.ReadOnlySerial("/dev/ttyACM0")

    with pytest.raises(MODULE.SerialDisconnected):
        serial.read(4096)
