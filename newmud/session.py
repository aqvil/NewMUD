"""Network side of one MUD connection. Runs the socket in a background
thread and hands decoded events to the GUI through a queue."""
import codecs
import queue
import socket
import threading

from .telnet import TelnetParser, escape_iac


class Connection:
    def __init__(self, host, port, encoding="auto", window_size=lambda: (80, 24)):
        self.host = host
        self.port = int(port)
        self.encoding = encoding
        self.events = queue.Queue()
        self.sock = None
        self.alive = False
        self._lock = threading.Lock()
        self.telnet = TelnetParser(window_size)
        self._decoder = self._make_decoder()
        self.bytes_in = 0

    def _make_decoder(self):
        enc = "utf-8" if self.encoding in ("auto", "", None) else self.encoding
        return codecs.getincrementaldecoder(enc)(errors="strict")

    def _decode(self, data):
        try:
            return self._decoder.decode(data)
        except UnicodeDecodeError:
            if self.encoding not in ("auto", "", None):
                return data.decode(self.encoding, errors="replace")
            # Old MUDs often send Latin-1 / CP437 bytes: fall back permanently.
            self.encoding = "cp1252"
            self._decoder = codecs.getincrementaldecoder("cp1252")(errors="replace")
            return self._decoder.decode(data)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=20)
            self.sock.settimeout(None)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError as e:
            self.events.put(("closed", f"Could not connect to {self.host}:{self.port} - {e}"))
            return
        self.alive = True
        self.events.put(("connected",))
        reason = "Connection closed by the server."
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                self.bytes_in += len(data)
                text_parts = []
                for ev in self.telnet.feed(data):
                    kind = ev[0]
                    if kind == "data":
                        text_parts.append(self._decode(ev[1]))
                    elif kind == "send":
                        self._send_raw(ev[1])
                    else:
                        if text_parts:
                            self.events.put(("text", "".join(text_parts)))
                            text_parts = []
                        self.events.put(ev)
                if text_parts:
                    self.events.put(("text", "".join(text_parts)))
        except OSError as e:
            if self.alive:
                reason = f"Connection lost: {e}"
        self.alive = False
        self.events.put(("closed", reason))

    def _send_raw(self, data):
        with self._lock:
            if self.sock:
                try:
                    self.sock.sendall(data)
                except OSError:
                    pass

    def send_line(self, text):
        enc = "utf-8" if self.encoding in ("auto", "", None) else self.encoding
        data = text.encode(enc, errors="replace")
        self._send_raw(escape_iac(data) + b"\r\n")

    def send_window_size(self):
        if self.telnet.naws_enabled:
            self._send_raw(self.telnet.naws_bytes())

    def close(self):
        self.alive = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
