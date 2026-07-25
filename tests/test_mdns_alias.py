from __future__ import annotations

import struct
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mdns_alias  # noqa: E402


def _query(name: str, *, unicast: bool) -> bytes:
    qclass = mdns_alias.CLASS_IN | (0x8000 if unicast else 0)
    return (
        b"\x12\x34"
        + b"\x00\x00"
        + struct.pack("!HHHH", 1, 0, 0, 0)
        + mdns_alias.encode_name(name)
        + struct.pack("!HH", mdns_alias.TYPE_A, qclass)
    )


def test_parses_multicast_question():
    assert mdns_alias.parse_questions(_query("stackchan-nanobot.local", unicast=False)) == [
        ("stackchan-nanobot.local.", mdns_alias.TYPE_A, mdns_alias.CLASS_IN, False)
    ]


def test_preserves_unicast_response_bit():
    assert mdns_alias.parse_questions(_query("stackchan-nanobot.local", unicast=True)) == [
        ("stackchan-nanobot.local.", mdns_alias.TYPE_A, mdns_alias.CLASS_IN, True)
    ]


def test_legacy_unicast_response_matches_lwip_dns_parser_requirements():
    query = _query("stackchan-nanobot.local", unicast=False)
    response = mdns_alias.make_response(
        query,
        "stackchan-nanobot.local",
        "192.168.137.247",
        120,
        cache_flush=False,
    )

    txid, flags, questions, answers, authority, additional = struct.unpack_from("!HHHHHH", response)
    assert txid == 0x1234
    assert flags & 0x8000
    assert (questions, answers, authority, additional) == (1, 1, 0, 0)

    question_name, offset = mdns_alias.read_name(response, 12)
    assert question_name == "stackchan-nanobot.local."
    qtype, qclass = struct.unpack_from("!HH", response, offset)
    assert (qtype, qclass) == (mdns_alias.TYPE_A, mdns_alias.CLASS_IN)

    answer_name, offset = mdns_alias.read_name(response, offset + 4)
    assert answer_name == "stackchan-nanobot.local."
    answer_type, answer_class, ttl, length = struct.unpack_from("!HHIH", response, offset)
    assert (answer_type, answer_class, ttl, length) == (
        mdns_alias.TYPE_A,
        mdns_alias.CLASS_IN,
        120,
        4,
    )
