"""Import worlds, triggers and F-key macros from a GMud 1.9 WorldLis.mud file.

GMud stores its data with MFC's CArchive serialization. We read just enough of
that format to recover the useful parts:
  CWorld   -> name, host, port
  CTrigger -> pattern, response
  CMacro   -> key, command
  CAlias   -> name, replacement
"""
import struct

from .storage import new_world


class _Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def left(self):
        return len(self.d) - self.p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def cstring(self):
        n = self.u8()
        if n == 0xFF:
            n = self.u16()
            if n == 0xFFFF:
                n = self.u32()
        s = self.d[self.p:self.p + n]
        self.p += n
        return s.decode("cp1252", errors="replace")

    def peek_tag(self):
        """True if the next two bytes are an MFC object tag (new class or class ref)."""
        if self.left() < 2:
            return False
        lo, hi = self.d[self.p], self.d[self.p + 1]
        return (lo == 0xFF and hi == 0xFF) or (hi & 0x80 and lo < 0x7F)

    def object_tag(self):
        """Read an object tag; returns class name if a new class is declared."""
        tag = self.u16()
        if tag == 0xFFFF:
            self.u16()  # schema
            n = self.u16()
            name = self.d[self.p:self.p + n].decode("ascii", errors="replace")
            self.p += n
            return name
        return None


def _records(data, class_name, fields):
    """Find the first object of class_name and read consecutive records of
    `fields` strings each, following MFC class-reference tags."""
    marker = class_name.encode("ascii")
    idx = data.find(b"\xff\xff", 0)
    out = []
    while idx != -1:
        r = _Reader(data)
        r.p = idx
        try:
            if r.object_tag() == class_name:
                break
        except (IndexError, struct.error):
            pass
        idx = data.find(b"\xff\xff", idx + 1)
    else:
        return out
    if idx == -1 or data.find(marker, idx) == -1:
        return out
    try:
        while True:
            rec = [r.cstring() for _ in range(fields)]
            out.append(rec)
            # optional trailing fields (e.g. trigger sound file) until next tag
            while r.left() >= 2 and not r.peek_tag():
                if r.d[r.p] == 0:
                    r.p += 1
                    if r.peek_tag():
                        break
                    return out
                else:
                    return out
            if not r.peek_tag():
                return out
            tag = r.u16()
            if tag == 0xFFFF or not tag & 0x8000:
                return out
    except (IndexError, struct.error):
        pass
    return out


def import_worldlist(path):
    """Return a list of NewMUD world dicts built from a GMud WorldLis.mud."""
    with open(path, "rb") as f:
        data = f.read()

    worlds = []
    r = _Reader(data)
    try:
        r.u16()  # file version
        count = r.u16()
        for _ in range(count):
            r.object_tag()
            name = r.cstring()
            host = r.cstring()
            port = r.u32()
            worlds.append(new_world(name or host, host, port if 0 < port < 65536 else 23))
            # skip description / set names until the next world tag or array
            for _ in range(8):
                if r.peek_tag() or r.left() < 1:
                    break
                r.cstring()
    except (IndexError, struct.error):
        pass

    triggers = [
        {"pattern": p, "type": "contains", "send": s, "highlight": "", "gag": False,
         "sound": "", "enabled": True}
        for p, s in _records(data, "CTrigger", 2) if p
    ]
    macros = [
        {"key": k.upper(), "command": c, "enabled": True}
        for k, c in _records(data, "CMacro", 2) if k
    ]
    aliases = [
        {"name": n, "replacement": rep, "enabled": True}
        for n, rep in _records(data, "CAlias", 2) if n
    ]

    for w in worlds:
        w["triggers"] = [dict(t) for t in triggers]
        w["aliases"] = [dict(a) for a in aliases]
        if macros:
            existing = {m["key"]: m for m in w["macros"]}
            for m in macros:
                existing[m["key"]] = dict(m)
            w["macros"] = list(existing.values())
    return worlds
