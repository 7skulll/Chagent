"""Packet metadata parsing never reads or reports payload bytes."""

import socket
import struct
import unittest

from chagent.hostexec.network_monitor import packet_target


class NetworkMonitorTests(unittest.TestCase):
    def test_ipv4_tcp_destination_and_size(self):
        ethernet = b"\x00" * 12 + b"\x08\x00"
        ip = bytes([0x45, 0]) + struct.pack("!H", 43) + b"\x00" * 5 + bytes([6]) + b"\x00" * 2
        ip += socket.inet_aton("172.20.0.2") + socket.inet_aton("93.184.215.14")
        tcp = struct.pack("!HH", 51000, 443) + b"\x00" * 16
        self.assertEqual(packet_target(ethernet + ip + tcp + b"abc"), ("93.184.215.14", 443, 43))

    def test_non_ip_and_short_packets_are_ignored(self):
        self.assertIsNone(packet_target(b"short"))
        self.assertIsNone(packet_target(b"\x00" * 12 + b"\x08\x06" + b"\x00" * 40))
