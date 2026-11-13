"""Telnet protocol handling for MUD connections.

Supports the options MUDs actually use:
  ECHO (password hiding), SGA, TTYPE/MTTS (announces 256 + true color),
  NAWS (window size), EOR/GA (prompt detection), MCCP2 (compression).
Everything else is politely refused.
"""
import zlib

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
GA, EOR_CMD, NOP = 249, 239, 241

OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_EOR, OPT_NAWS = 1, 3, 24, 25, 31
OPT_MCCP2 = 86

TTYPE_IS, TTYPE_SEND = 0, 1

# MTTS bitfield: 1 ANSI, 4 UTF-8, 8 256 colors, 256 truecolor
TTYPE_CYCLE = [b"NEWMUD", b"XTERM-256COLOR", b"MTTS 269"]


def escape_iac(data: bytes) -> bytes:
    return data.replace(bytes([IAC]), bytes([IAC, IAC]))


class TelnetParser:
    """Feed raw socket bytes, get back events.

    Events are tuples:
      ("data", bytes)      plain text bytes for display
      ("prompt",)          server marked end of prompt (GA / EOR)
      ("echo", bool)       True = client should echo input, False = hide (password)
      ("send", bytes)      bytes the client must send back to the server
      ("compress", bool)   MCCP2 started / stopped (informational)
    """

    def __init__(self, get_window_size=lambda: (80, 24)):
        self.get_window_size = get_window_size
        self._buf = b""
        self._zlib = None
        self._ttype_index = 0
        self.naws_enabled = False
        self.compressed = False

    # ------------------------------------------------------------------ public
    def feed(self, raw: bytes):
        events = []
        if self._zlib is not None:
            raw = self._decompress(raw, events)
        self._buf += raw
        self._parse(events)
        return self._merge(events)

    def naws_bytes(self):
        cols, rows = self.get_window_size()
        payload = bytes([cols >> 8 & 255, cols & 255, rows >> 8 & 255, rows & 255])
        return bytes([IAC, SB, OPT_NAWS]) + escape_iac(payload) + bytes([IAC, SE])

    # ----------------------------------------------------------------- private
    def _decompress(self, raw, events):
        try:
            out = self._zlib.decompress(raw)
        except zlib.error:
            self._zlib = None
            self.compressed = False
            events.append(("compress", False))
            return b""
        if self._zlib.eof:
            leftover = self._zlib.unused_data
            self._zlib = None
            self.compressed = False
            events.append(("compress", False))
            out += leftover
        return out

    @staticmethod
    def _merge(events):
        merged = []
        for ev in events:
            if ev[0] == "data" and merged and merged[-1][0] == "data":
                merged[-1] = ("data", merged[-1][1] + ev[1])
            elif ev[0] == "data" and not ev[1]:
                continue
            else:
                merged.append(ev)
        return merged

    def _parse(self, events):
        buf = self._buf
        i = 0
        start = 0
        n = len(buf)
        while i < n:
            if buf[i] != IAC:
                i += 1
                continue
            # flush text before IAC
            if i > start:
                events.append(("data", buf[start:i]))
            start = i
            if i + 1 >= n:
                break  # incomplete
            cmd = buf[i + 1]
            if cmd == IAC:
                events.append(("data", bytes([IAC])))
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    break
                self._negotiate(cmd, buf[i + 2], events)
                i += 3
            elif cmd == SB:
                end = self._find_se(buf, i + 2)
                if end == -1:
                    break
                payload = buf[i + 2:end].replace(bytes([IAC, IAC]), bytes([IAC]))
                i = end + 2
                if self._subneg(payload, events):
                    # MCCP2 starts right after this IAC SE: rest is compressed
                    rest = buf[i:]
                    self._buf = b""
                    plain = self._decompress(rest, events)
                    self._buf = plain
                    self._parse(events)
                    return
            elif cmd in (GA, EOR_CMD):
                events.append(("prompt",))
                i += 2
            else:
                i += 2  # NOP and friends
            start = i
        else:
            if n > start:
                events.append(("data", buf[start:n]))
            self._buf = b""
            return
        # loop broke on an incomplete sequence: keep it for next feed
        if i > start:
            events.append(("data", buf[start:i]))
        self._buf = buf[i:]

    @staticmethod
    def _find_se(buf, pos):
        while True:
            j = buf.find(bytes([IAC]), pos)
            if j == -1 or j + 1 >= len(buf):
                return -1
            if buf[j + 1] == SE:
                return j
            pos = j + 2

    def _reply(self, events, cmd, opt):
        events.append(("send", bytes([IAC, cmd, opt])))

    def _negotiate(self, cmd, opt, events):
        if cmd == WILL:
            if opt == OPT_ECHO:
                self._reply(events, DO, opt)
                events.append(("echo", False))
            elif opt in (OPT_SGA, OPT_EOR, OPT_MCCP2):
                self._reply(events, DO, opt)
            else:
                self._reply(events, DONT, opt)
        elif cmd == WONT:
            if opt == OPT_ECHO:
                self._reply(events, DONT, opt)
                events.append(("echo", True))
        elif cmd == DO:
            if opt == OPT_TTYPE:
                self._ttype_index = 0
                self._reply(events, WILL, opt)
            elif opt == OPT_NAWS:
                self._reply(events, WILL, opt)
                self.naws_enabled = True
                events.append(("send", self.naws_bytes()))
            elif opt == OPT_SGA:
                self._reply(events, WILL, opt)
            else:
                self._reply(events, WONT, opt)
        elif cmd == DONT:
            if opt == OPT_NAWS:
                self.naws_enabled = False
            self._reply(events, WONT, opt)

    def _subneg(self, payload, events):
        """Handle a subnegotiation. Returns True if MCCP2 compression begins."""
        if not payload:
            return False
        opt = payload[0]
        if opt == OPT_TTYPE and len(payload) > 1 and payload[1] == TTYPE_SEND:
            name = TTYPE_CYCLE[min(self._ttype_index, len(TTYPE_CYCLE) - 1)]
            self._ttype_index += 1
            events.append(("send", bytes([IAC, SB, OPT_TTYPE, TTYPE_IS]) + name + bytes([IAC, SE])))
        elif opt == OPT_MCCP2:
            self._zlib = zlib.decompressobj()
            self.compressed = True
            events.append(("compress", True))
            return True
        return False
