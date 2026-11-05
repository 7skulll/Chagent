"""Observe IP destinations and byte counts in one workspace network namespace.

This process has CAP_NET_RAW only, no workspace mount and no Docker socket. It
reports packet metadata, never payloads. The host controller owns its lifetime.
"""

import ipaddress
import json
import signal
import socket
import struct
import sys
from collections import defaultdict


def packet_target(packet: bytes) -> tuple[str, int, int] | None:
    if len(packet) < 14:
        return None
    offset = 14
    protocol = struct.unpack_from("!H", packet, 12)[0]
    if protocol == 0x8100 and len(packet) >= 18:
        protocol = struct.unpack_from("!H", packet, 16)[0]
        offset = 18
    if protocol == 0x0800 and len(packet) >= offset + 20:
        header = (packet[offset] & 15) * 4
        if header < 20 or len(packet) < offset + header:
            return None
        destination = str(ipaddress.IPv4Address(packet[offset + 16:offset + 20]))
        transport = packet[offset + 9]
        length = min(len(packet) - offset, struct.unpack_from("!H", packet, offset + 2)[0])
    elif protocol == 0x86DD and len(packet) >= offset + 40:
        header = 40
        destination = str(ipaddress.IPv6Address(packet[offset + 24:offset + 40]))
        transport = packet[offset + 6]
        length = min(len(packet) - offset, struct.unpack_from("!H", packet, offset + 4)[0] + 40)
    else:
        return None
    port = (struct.unpack_from("!H", packet, offset + header + 2)[0]
            if transport in (6, 17) and len(packet) >= offset + header + 4 else 0)
    return destination, port, length


def main() -> None:
    running = True

    def stop(_signal, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    sock.bind(("eth0", 0))
    sock.settimeout(.2)
    targets: dict[tuple[str, int], int] = defaultdict(int)
    packets = 0
    outgoing_bytes = 0
    truncated = False
    print("READY", flush=True)
    try:
        while running:
            try:
                packet, address = sock.recvfrom(65535)
            except TimeoutError:
                continue
            if address[2] != socket.PACKET_OUTGOING:
                continue
            parsed = packet_target(packet)
            if parsed is None:
                continue
            destination, port, length = parsed
            packets += 1
            outgoing_bytes += length
            key = (destination, port)
            if key in targets or len(targets) < 10_000:
                targets[key] += length
            else:
                truncated = True
    finally:
        sock.close()
    print(json.dumps({"targets": [{"ip": ip, "port": port, "sent_bytes": size}
                                  for (ip, port), size in sorted(targets.items())],
                      "sent_bytes": outgoing_bytes, "packets": packets,
                      "truncated": truncated}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"MONITOR_ERROR:{type(error).__name__}", file=sys.stderr, flush=True)
        raise
