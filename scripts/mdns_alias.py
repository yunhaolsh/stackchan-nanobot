#!/usr/bin/env python3
import argparse
import ipaddress
import socket
import struct
import time


MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
TYPE_A = 1
TYPE_ANY = 255
CLASS_IN = 1


def encode_name(name):
    name = name.rstrip(".")
    out = bytearray()
    for part in name.split("."):
        raw = part.encode("utf-8")
        if len(raw) > 63:
            raise ValueError(f"DNS label too long: {part}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def read_name(packet, offset):
    labels = []
    jumped = False
    end = offset
    seen = set()
    while True:
        if offset >= len(packet):
            raise ValueError("name exceeds packet")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("bad compression pointer")
            ptr = ((length & 0x3F) << 8) | packet[offset + 1]
            if ptr in seen:
                raise ValueError("compression loop")
            seen.add(ptr)
            if not jumped:
                end = offset + 2
            offset = ptr
            jumped = True
            continue
        if length == 0:
            if not jumped:
                end = offset + 1
            break
        offset += 1
        labels.append(packet[offset:offset + length].decode("utf-8", errors="ignore"))
        offset += length
    return ".".join(labels).lower() + ".", end


def parse_questions(packet):
    if len(packet) < 12:
        return []
    qdcount = struct.unpack_from("!H", packet, 4)[0]
    offset = 12
    questions = []
    for _ in range(qdcount):
        name, offset = read_name(packet, offset)
        if offset + 4 > len(packet):
            break
        qtype, raw_qclass = struct.unpack_from("!HH", packet, offset)
        offset += 4
        questions.append((name, qtype, raw_qclass & 0x7FFF, bool(raw_qclass & 0x8000)))
    return questions


def make_response(packet, name, address, ttl, question_type=TYPE_A, cache_flush=False):
    txid = packet[:2] if len(packet) >= 2 else b"\x00\x00"
    flags = b"\x84\x00"
    counts = struct.pack("!HHHH", 1, 1, 0, 0)
    question = encode_name(name)
    question += struct.pack("!HH", question_type, CLASS_IN)
    answer = encode_name(name)
    answer_class = CLASS_IN | (0x8000 if cache_flush else 0)
    answer += struct.pack("!HHIH", TYPE_A, answer_class, ttl, 4)
    answer += ipaddress.IPv4Address(address).packed
    return txid + flags + counts + question + answer


def make_announcement(name, address, ttl):
    flags = b"\x84\x00"
    counts = struct.pack("!HHHH", 0, 1, 0, 0)
    answer = encode_name(name)
    answer += struct.pack("!HHIH", TYPE_A, 0x8000 | CLASS_IN, ttl, 4)
    answer += ipaddress.IPv4Address(address).packed
    return b"\x00\x00" + flags + counts + answer


def open_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.bind(("", MDNS_PORT))
    mreq = socket.inet_aton(MDNS_ADDR) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(1.0)
    return sock


def main():
    parser = argparse.ArgumentParser(description="Publish a simple .local IPv4 alias via mDNS.")
    parser.add_argument("--name", default="stackchan-nanobot.local")
    parser.add_argument("--address", required=True)
    parser.add_argument("--ttl", type=int, default=120)
    args = parser.parse_args()

    name = args.name.rstrip(".").lower() + "."
    ipaddress.IPv4Address(args.address)
    sock = open_socket()
    print(f"[mdns] publishing {name} -> {args.address}", flush=True)

    last_announce = 0.0
    while True:
        now = time.monotonic()
        if now - last_announce > 30:
            response = make_announcement(name, args.address, args.ttl)
            sock.sendto(response, (MDNS_ADDR, MDNS_PORT))
            last_announce = now
        try:
            packet, peer = sock.recvfrom(1500)
        except socket.timeout:
            continue
        try:
            questions = parse_questions(packet)
        except ValueError:
            continue
        for qname, qtype, qclass, wants_unicast in questions:
            if qname == name and qclass == CLASS_IN and qtype in (TYPE_A, TYPE_ANY):
                # DNS clients commonly send mDNS queries from an ephemeral port
                # and set the QU bit. They cannot receive a multicast reply sent
                # only to port 5353, so honor either signal with a unicast reply.
                reply_target = peer if wants_unicast or peer[1] != MDNS_PORT else (MDNS_ADDR, MDNS_PORT)
                reply_mode = "unicast" if reply_target == peer else "multicast"
                response = make_response(
                    packet,
                    name,
                    args.address,
                    args.ttl,
                    question_type=qtype,
                    cache_flush=reply_mode == "multicast",
                )
                print(
                    f"[mdns] query from {peer[0]}:{peer[1]} for {name} -> "
                    f"{args.address} ({reply_mode})",
                    flush=True,
                )
                sock.sendto(response, reply_target)
                break


if __name__ == "__main__":
    main()
