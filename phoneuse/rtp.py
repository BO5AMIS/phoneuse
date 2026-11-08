"""H.264 over RTP (RFC 6184) back into access units, for the Windows video hub.

UxPlay sends the phone's stream as RTP to 127.0.0.1 (`-vrtp`). This reassembles
single NAL unit packets, STAP-A aggregates and FU-A fragments into Annex B access
units (start code before each NAL unit), one per frame. A packet lost on the way
drops the NAL unit it belonged to and is counted; the decoder conceals the rest.
"""
import struct

START = b"\x00\x00\x00\x01"


class Depacketizer:
    def __init__(self):
        self.nals = []  # NAL units of the access unit being collected
        self.ts = None  # its RTP timestamp
        self.ssrc = None
        self.seq = None
        self.fu = None  # bytearray of the FU-A NAL unit being reassembled
        self.lost = 0  # packets missing (sequence gaps)
        self.restarts = 0  # new streams (SSRC changes: UxPlay or the phone started over)

    def push(self, pkt):
        """Feed one RTP packet; returns a list of completed access units (bytes)."""
        out = []
        if len(pkt) < 12 or pkt[0] >> 6 != 2:
            return out
        cc, ext, pad, marker = pkt[0] & 0x0F, pkt[0] & 0x10, pkt[0] & 0x20, pkt[1] & 0x80
        seq, ts, ssrc = struct.unpack_from("!HII", pkt, 2)
        off = 12 + 4 * cc
        if ext:
            if len(pkt) < off + 4:
                return out
            off += 4 + 4 * struct.unpack_from("!H", pkt, off + 2)[0]
        end = len(pkt) - (pkt[-1] if pad else 0)
        payload = pkt[off:end]
        if not payload:
            return out

        if ssrc != self.ssrc:
            if self.ssrc is not None:
                self.restarts += 1
            self.ssrc, self.seq, self.ts, self.nals, self.fu = ssrc, None, None, [], None
        if self.seq is not None:
            gap = (seq - self.seq - 1) & 0xFFFF
            if gap and gap < 0x8000:
                self.lost += gap
                self.fu = None  # the fragment in progress is incomplete
            elif gap >= 0x8000:
                return out  # a late duplicate or reordered packet: too late for it
        self.seq = seq
        if self.ts is not None and ts != self.ts and self.nals:
            out.append(self._flush())  # the previous frame's marker went missing
        self.ts = ts

        kind = payload[0] & 0x1F
        if 1 <= kind <= 23:
            self.nals.append(bytes(payload))
        elif kind == 24:  # STAP-A: 16-bit size, NAL unit, repeated
            i = 1
            while i + 2 <= len(payload):
                n = struct.unpack_from("!H", payload, i)[0]
                i += 2
                if n == 0 or i + n > len(payload):
                    break
                self.nals.append(bytes(payload[i:i + n]))
                i += n
        elif kind == 28 and len(payload) >= 2:  # FU-A
            start, stop = payload[1] & 0x80, payload[1] & 0x40
            if start:
                self.fu = bytearray([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
            if self.fu is not None:
                self.fu += payload[2:]
                if stop:
                    self.nals.append(bytes(self.fu))
                    self.fu = None
        if marker and self.nals:
            out.append(self._flush())
        return out

    def _flush(self):
        au = b"".join(START + n for n in self.nals)
        self.nals = []
        return au


def nal_types(au):
    """NAL unit types in an Annex B access unit (for tests and logs)."""
    types, i = [], 0
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(au):
            return types
        types.append(au[i + 3] & 0x1F)
        i += 3
