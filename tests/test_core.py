import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from newmud.ansi import AnsiParser, Style, strip_ansi  # noqa: E402
from newmud.automation import Automation, expand_speedwalk, split_stack  # noqa: E402
from newmud.telnet import (DO, IAC, OPT_ECHO, OPT_MCCP2, OPT_NAWS, OPT_TTYPE, SB, SE,  # noqa: E402
                           TTYPE_SEND, WILL, WONT, GA, TelnetParser)


class AnsiTests(unittest.TestCase):
    def test_256_color(self):
        segs = AnsiParser().feed("a \x1b[1;38;5;165mpink\x1b[0m end")
        self.assertEqual([t for t, _ in segs], ["a ", "pink", " end"])
        self.assertEqual(segs[1][1].fg, 165)
        self.assertTrue(segs[1][1].bold)
        self.assertEqual(segs[2][1], Style())

    def test_truecolor(self):
        segs = AnsiParser().feed("\x1b[38;2;255;128;0morange")
        self.assertEqual(segs[0][1].fg, "#ff8000")

    def test_split_sequence(self):
        p = AnsiParser()
        a = p.feed("red \x1b[38;5;1")
        b = p.feed("60mmaroon")
        self.assertEqual("".join(t for t, _ in a + b), "red maroon")
        self.assertEqual(b[0][1].fg, 160)

    def test_strip_cursor_codes(self):
        self.assertEqual(strip_ansi("\x1b[2J\x1b[Hhello\r\n"), "hello\n")


class TelnetTests(unittest.TestCase):
    def test_plain_and_iac_escape(self):
        ev = TelnetParser().feed(b"hi" + bytes([IAC, IAC]) + b"!")
        self.assertEqual(ev, [("data", b"hi\xff!")])

    def test_echo_password(self):
        ev = TelnetParser().feed(bytes([IAC, WILL, OPT_ECHO]) + b"Password: ")
        self.assertIn(("echo", False), ev)
        self.assertIn(("send", bytes([IAC, DO, OPT_ECHO])), ev)
        ev = TelnetParser().feed(bytes([IAC, WONT, OPT_ECHO]))
        self.assertIn(("echo", True), ev)

    def test_split_negotiation(self):
        p = TelnetParser()
        a = p.feed(b"abc" + bytes([IAC]))
        b = p.feed(bytes([GA]) + b"def")
        self.assertEqual(a, [("data", b"abc")])
        self.assertEqual(b, [("prompt",), ("data", b"def")])

    def test_ttype_cycle(self):
        p = TelnetParser()
        p.feed(bytes([IAC, DO, OPT_TTYPE]))
        req = bytes([IAC, SB, OPT_TTYPE, TTYPE_SEND, IAC, SE])
        names = [p.feed(req)[0][1][4:-2] for _ in range(4)]
        self.assertEqual(names, [b"NEWMUD", b"XTERM-256COLOR", b"MTTS 269", b"MTTS 269"])

    def test_naws(self):
        p = TelnetParser(lambda: (120, 40))
        ev = p.feed(bytes([IAC, DO, OPT_NAWS]))
        self.assertIn(("send", bytes([IAC, SB, OPT_NAWS, 0, 120, 0, 40, IAC, SE])), ev)

    def test_mccp2(self):
        p = TelnetParser()
        comp = zlib.compressobj()
        body = comp.compress(b"compressed hello") + comp.flush(zlib.Z_SYNC_FLUSH)
        ev = p.feed(bytes([IAC, WILL, OPT_MCCP2]) + b"before" + bytes([IAC, SB, OPT_MCCP2, IAC, SE]) + body[:5])
        ev += p.feed(body[5:])
        text = b"".join(e[1] for e in ev if e[0] == "data")
        self.assertEqual(text, b"beforecompressed hello")
        self.assertTrue(p.compressed)


class AutomationTests(unittest.TestCase):
    def make(self, **world):
        return Automation(world, {"stack_char": ";", "speedwalk_prefix": ".", "command_char": "#"})

    def test_stack_and_walk(self):
        self.assertEqual(split_stack("n;get all;;x"), ["n", "get all;x"])
        self.assertEqual(expand_speedwalk("3n2e"), ["n", "n", "n", "e", "e"])
        self.assertIsNone(expand_speedwalk("hello"))
        acts = self.make().process_input(".2nw;look")
        self.assertEqual(acts, [("send", "nw"), ("send", "nw"), ("send", "look")])

    def test_alias(self):
        a = self.make(aliases=[{"name": "k", "replacement": "kill %1"},
                               {"name": "gh", "replacement": "c 'group heal'"},
                               {"name": "loop", "replacement": "loop"}])
        self.assertEqual(a.process_input("k rat"), [("send", "kill rat")])
        self.assertEqual(a.process_input("gh"), [("send", "c 'group heal'")])
        self.assertEqual(a.process_input("loop")[-1][0], "client")  # loop guard

    def test_repeat_and_vars(self):
        a = self.make()
        self.assertEqual(len(a.process_input("#3 bash")), 3)
        a.process_input("#var target goblin")
        self.assertEqual(a.process_input("kill @target"), [("send", "kill goblin")])

    def test_triggers(self):
        a = self.make(triggers=[
            {"pattern": "You can't fight while sitting!", "type": "contains", "send": "stand"},
            {"pattern": "* tells you '*'", "type": "wildcard", "send": "reply thanks %1", "highlight": "#ff0"},
            {"pattern": "^spam", "type": "regex", "gag": True},
        ])
        self.assertEqual(a.check_line("You can't fight while sitting!")["commands"], ["stand"])
        r = a.check_line("Bob tells you 'hi'")
        self.assertEqual(r["commands"], ["reply thanks Bob"])
        self.assertEqual(r["highlights"][0][2], "#ff0")
        self.assertTrue(a.check_line("spam spam")["gag"])


if __name__ == "__main__":
    unittest.main()
