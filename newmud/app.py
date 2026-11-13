"""NewMUD main window."""
import datetime
import html
import os
import queue
import re
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .ansi import DEFAULT_PALETTE, AnsiParser, xterm_color
from .automation import Automation
from .dialogs import ListEditor, SettingsDialog, WorldManager, center
from .gmud_import import import_worldlist
from .session import Connection
from .storage import DEFAULT_GAUGES, Store, app_dir, new_world

URL_RE = re.compile(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+\.[a-z]{2,}[^\s<>\"']*)", re.IGNORECASE)

# Windows virtual key codes of the numeric keypad
NUMPAD_KEYCODES = {96 + i: f"Num{i}" for i in range(10)}
NUMPAD_KEYCODES.update({106: "Num*", 107: "Num+", 109: "Num-", 110: "Num.", 111: "Num/"})
NUMPAD_KEYSYMS = {f"KP_{i}": f"Num{i}" for i in range(10)}
NUMPAD_KEYSYMS.update({"KP_Up": "Num8", "KP_Down": "Num2", "KP_Left": "Num4", "KP_Right": "Num6",
                       "KP_Prior": "Num9", "KP_Next": "Num3", "KP_Home": "Num7", "KP_End": "Num1",
                       "KP_Begin": "Num5", "KP_Add": "Num+", "KP_Subtract": "Num-",
                       "KP_Multiply": "Num*", "KP_Divide": "Num/"})
NUMPAD_WALK = {"Num8": "n", "Num2": "s", "Num4": "w", "Num6": "e", "Num9": "u", "Num3": "d",
               "Num7": "nw", "Num1": "sw", "Num5": "look"}

HELP_TEXT = """NewMUD quick guide

TYPING
  north;get all;look       Several commands at once (separator ;)
  .3n2e                    Speedwalk: north x3, east x2
  #5 bash                  Repeat a command 5 times
  #var target goblin       Set a variable, then use it:  kill @target
  \\raw text               Send text exactly as typed (no aliases)
  Up / Down                Command history      Tab   Complete words seen on screen
  Ctrl+Up / Ctrl+Down      Search history by what you've typed

CLIENT COMMANDS
  #connect  #disconnect  #reconnect  #log  #clear  #echo text  #var  #unvar name
  #gag / #nogag   (toggle hiding of gag triggers)

SCROLLBACK
  Mouse wheel or PageUp opens a split view: read the history at the top
  while new text keeps arriving below. Scroll to the bottom or press
  Ctrl+End / Escape to close it.  Ctrl+F searches the scrollback.

AUTOMATION (World menu)
  Aliases   k = kill %1      or   k = kill  (arguments are appended)
  Triggers  'contains', 'wildcard' (* and ?), 'regex' or 'exact' patterns.
            Send commands (%1..%9 = captured text), highlight in a color,
            gag (hide) the line or play a sound.
  Macros    F1-F12, Shift/Ctrl+F-keys and the numeric keypad.
  Timers    Send commands every N seconds.
  Gauges    Health/mana/move bars read from your prompt.

Copying: select text with the mouse, it is copied when you release the button.
"""


def _font_tuple(settings, *styles):
    return (settings["font_family"], settings["font_size"]) + styles


class SessionTab(ttk.Frame):
    """One world connection: output, input, gauges, automation."""

    def __init__(self, app, world):
        super().__init__(app.notebook)
        self.app = app
        self.world = world
        self.settings = app.store.settings
        self.auto = Automation(world, self.settings)
        self.parser = AnsiParser()
        self.conn = None
        self.connected = False
        self.password_mode = False
        self.log_file = None
        self.tag_cache = {}
        self.line_text = ""
        self.line_triggered = False
        self.skip_newline = False
        self.gag_enabled = True
        self.history = world.setdefault("history", [])
        self.hist_pos = len(self.history)
        self.hist_draft = ""
        self.complete_state = None
        self.timer_jobs = []
        self.gauge_values = {}
        self.gauge_max = {}
        self.unseen = False
        self.manual_close = False
        self.reconnect_job = None
        self.connected_at = None
        self.split_open = False
        self._build()
        self.apply_settings()
        self.start_timers()

    # ------------------------------------------------------------------ build
    def _build(self):
        s = self.settings
        self.pane = tk.PanedWindow(self, orient="vertical", sashwidth=6, bd=0,
                                   sashrelief="raised", bg="#333333")
        self.pane.pack(fill="both", expand=True)

        live_frame = tk.Frame(self.pane, bg=s["background"])
        self.out = tk.Text(live_frame, wrap="word", bd=0, highlightthickness=0, padx=6, pady=4,
                           cursor="xterm", undo=False, exportselection=True)
        self.out.pack(side="left", fill="both", expand=True)

        # Peer text widget that shares the same content: the scrollback view.
        self.back_frame = tk.Frame(self.pane, bg=s["background"])
        self.back = tk.Text.__new__(tk.Text)
        tk.BaseWidget._setup(self.back, self.back_frame, {})
        self.out.tk.call(self.out._w, "peer", "create", self.back._w, "-bd", 0, "-highlightthickness", 0,
                         "-padx", 6, "-pady", 4)
        back_sb = ttk.Scrollbar(self.back_frame, orient="vertical", command=self.back.yview)
        self.back.configure(yscrollcommand=lambda a, b: (back_sb.set(a, b), self._back_scrolled(a, b)))
        self.back.pack(side="left", fill="both", expand=True)
        back_sb.pack(side="left", fill="y")
        self.pane.add(live_frame, stretch="always")

        for w in (self.out, self.back):
            w.bind("<MouseWheel>", self._on_wheel)
            w.bind("<Button-4>", lambda e: self._scroll_back(-3))
            w.bind("<Button-5>", lambda e: self._scroll_back(3))
            w.bind("<Key>", self._output_key)
            w.bind("<ButtonRelease-1>", self._copy_selection)
        self.out.mark_set("nm_ls", "end-1c")
        self.out.mark_gravity("nm_ls", "left")

        self.out.tag_configure("echo")
        self.out.tag_configure("system")
        self.out.tag_configure("link", underline=True)
        self.out.tag_bind("link", "<Enter>", lambda e: e.widget.configure(cursor="hand2"))
        self.out.tag_bind("link", "<Leave>", lambda e: e.widget.configure(cursor="xterm"))
        self.out.tag_bind("link", "<Button-1>", self._open_link)
        self.out.tag_configure("found", background="#5f5f00", foreground="#ffffff")

        # gauges
        self.gauge_bar = tk.Frame(self, height=22)
        self.gauge_bar.pack(fill="x")
        self.gauge_widgets = {}

        self.separator = tk.Frame(self, height=2, bg="#2e8b57")
        self.separator.pack(fill="x")

        # input row
        row = tk.Frame(self)
        row.pack(fill="x")
        self.input = tk.Entry(row, bd=0, relief="flat", insertwidth=2)
        self.input.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 0))
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<KP_Enter>", self._on_enter)
        self.input.bind("<Up>", lambda e: self._history(-1))
        self.input.bind("<Down>", lambda e: self._history(1))
        self.input.bind("<Control-Up>", lambda e: self._history_search(-1))
        self.input.bind("<Control-Down>", lambda e: self._history_search(1))
        self.input.bind("<Prior>", lambda e: self._page(-1))
        self.input.bind("<Next>", lambda e: self._page(1))
        self.input.bind("<Control-End>", lambda e: self.close_split())
        self.input.bind("<Escape>", self._on_escape)
        self.input.bind("<Tab>", self._complete)
        self.input.bind("<Key>", self._input_key, add="+")
        self.input.bind("<MouseWheel>", self._on_wheel)
        self.out.bind("<Configure>", self._on_resize)

    def apply_settings(self):
        s = self.settings
        font = _font_tuple(s)
        for w in (self.out, self.back):
            w.configure(font=font, bg=s["background"], fg=s["foreground"],
                        insertbackground=s["foreground"], selectbackground="#264f78",
                        selectforeground="#ffffff", wrap="word" if s["word_wrap"] else "char")
        self.input.configure(font=font, bg=s["input_background"], fg=s["foreground"],
                             insertbackground=s["foreground"])
        self.out.tag_configure("echo", foreground=s["echo_color"])
        self.out.tag_configure("system", foreground=s["system_color"])
        self.out.tag_configure("italic", font=_font_tuple(s, "italic"))
        self.gauge_bar.configure(bg=s["input_background"])
        self.tag_cache.clear()
        self.auto = Automation(self.world, s)
        self.rebuild_gauges()

    # -------------------------------------------------------------- connection
    def connect(self):
        if self.conn and self.conn.alive:
            return
        self.manual_close = False
        if self.reconnect_job:
            self.after_cancel(self.reconnect_job)
            self.reconnect_job = None
        self.parser.reset()
        self.password_mode = False
        self.input.configure(show="")
        self.system(f"Connecting to {self.world['host']}:{self.world['port']} …")
        self.conn = Connection(self.world["host"], self.world["port"], self.world.get("encoding", "auto"),
                               self.window_size)
        self.conn.start()
        self.app.update_status()

    def disconnect(self):
        self.manual_close = True
        if self.reconnect_job:
            self.after_cancel(self.reconnect_job)
            self.reconnect_job = None
        if self.conn:
            self.conn.close()

    def reconnect(self):
        self.disconnect()
        self.after(300, self.connect)

    def window_size(self):
        try:
            f = tkfont.Font(font=self.out.cget("font"))
            cw = max(f.measure("0"), 1)
            lh = max(f.metrics("linespace"), 1)
            cols = max(20, (self.out.winfo_width() - 12) // cw)
            rows = max(5, (self.out.winfo_height() - 8) // lh)
            return min(cols, 500), min(rows, 300)
        except tk.TclError:
            return 80, 24

    def _on_resize(self, _):
        if getattr(self, "_resize_job", None):
            self.after_cancel(self._resize_job)

        def send():
            self._resize_job = None
            if self.conn and self.conn.alive:
                self.conn.send_window_size()
        self._resize_job = self.after(400, send)

    def poll(self):
        """Called by the app loop: drain network events."""
        if not self.conn:
            return False
        got = False
        deadline = time.perf_counter() + 0.05
        while time.perf_counter() < deadline:
            try:
                ev = self.conn.events.get_nowait()
            except queue.Empty:
                break
            got = True
            kind = ev[0]
            if kind == "text":
                self.on_text(ev[1])
            elif kind == "prompt":
                self.on_prompt()
            elif kind == "echo":
                self.password_mode = not ev[1]
                self.input.configure(show="•" if self.password_mode else "")
            elif kind == "connected":
                self.connected = True
                self.connected_at = time.time()
                self.system("Connected.")
                self.app.update_status()
                for line in self.world.get("connect_commands", "").splitlines():
                    if line.strip():
                        self.send_command(line, echo=False)
            elif kind == "closed":
                self.connected = False
                self.password_mode = False
                self.input.configure(show="")
                self.flush_partial()
                self.system(ev[1])
                self.app.update_status()
                if self.settings.get("auto_reconnect") and not self.manual_close and self.connected_at:
                    self.system("Reconnecting in 5 seconds… (#disconnect to cancel)")
                    self.reconnect_job = self.after(5000, self.connect)
                self.connected_at = None
            elif kind == "compress":
                self.app.update_status()
        if got:
            self._trim()
            if self.app.current_tab() is not self:
                self.unseen = True
                self.app.mark_tab(self)
        return got

    # ------------------------------------------------------------------ output
    def _tag_for(self, style):
        key = style
        tag = self.tag_cache.get(key)
        if tag:
            return tag
        s = self.settings
        fg, bg = style.fg, style.bg
        bright = s.get("bold_is_bright", True)
        if isinstance(fg, int):
            if fg < 8 and style.bold and bright:
                fg += 8
            fg = xterm_color(fg, DEFAULT_PALETTE)
        elif fg is None and style.bold and bright:
            fg = DEFAULT_PALETTE[15]
        if isinstance(bg, int):
            bg = xterm_color(bg, DEFAULT_PALETTE)
        if style.reverse:
            fg, bg = (bg or s["background"]), (fg or s["foreground"])
        opts = {}
        if fg:
            opts["foreground"] = fg
        if bg:
            opts["background"] = bg
        if style.underline:
            opts["underline"] = True
        if style.italic:
            opts["font"] = _font_tuple(s, "italic")
        self._tag_counter = getattr(self, "_tag_counter", 0) + 1
        tag = f"st{self._tag_counter}"
        self.out.tag_configure(tag, **opts)
        self.out.tag_lower(tag, "echo")
        self.tag_cache[key] = tag
        return tag

    def on_text(self, text):
        for chunk, style in self.parser.feed(text):
            tag = self._tag_for(style)
            parts = chunk.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    if self.skip_newline and not self.line_text:
                        self.skip_newline = False
                    else:
                        self.out.insert("end", "\n")
                        self.finish_line()
                if part:
                    self.skip_newline = False
                    self.out.insert("end", part, tag)
                    self.line_text += part
        if self.line_text:
            self.check_gauges(self.line_text)
        self.out.see("end")

    def on_prompt(self):
        if self.line_text and not self.line_triggered:
            self.check_gauges(self.line_text)
            self._run_triggers(self.line_text, partial=True)
            self.line_triggered = True

    def flush_partial(self):
        if self.line_text:
            self.out.insert("end", "\n")
            self.finish_line()

    def finish_line(self):
        line = self.line_text
        self.line_text = ""
        start = "nm_ls"
        gagged = False
        if not self.line_triggered:
            gagged = self._run_triggers(line)
        if gagged:
            self.out.delete(start, "end-1c")
        else:
            if self.settings.get("clickable_links") and ("http" in line or "www." in line):
                for m in URL_RE.finditer(line):
                    self.out.tag_add("link", f"{start}+{m.start()}c", f"{start}+{m.end()}c")
            if self.log_file:
                try:
                    self.log_file.write(line + "\n")
                except OSError:
                    pass
        self.line_triggered = False
        self.out.mark_set("nm_ls", "end-1c")

    def _run_triggers(self, line, partial=False):
        if not self.auto.triggers:
            return False
        res = self.auto.check_line(line)
        for a, b, color in res["highlights"]:
            tag = f"hl_{color}"
            self.out.tag_configure(tag, foreground=color)
            self.out.tag_raise(tag)
            self.out.tag_add(tag, f"nm_ls+{a}c", f"nm_ls+{b}c")
        for snd in res["sounds"]:
            self.play_sound(snd)
        for cmd in res["commands"]:
            self.after_idle(lambda c=cmd: self.send_command(c, echo=True, from_trigger=True))
        if res["commands"] or res["highlights"] or res["sounds"]:
            if self.app.current_tab() is not self:
                self.app.mark_tab(self, alert=True)
        return res["gag"] and self.gag_enabled and not partial

    def play_sound(self, path):
        if sys.platform == "win32" and path and os.path.exists(path):
            try:
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            except (RuntimeError, ImportError):
                pass
        self.bell()

    def _insert_client_line(self, text, tag):
        if self.line_text:
            # keep partial (prompt) line intact: client text goes on its own line
            self.out.insert("end", "\n")
            self.finish_line()
        self.out.insert("end", text + "\n", tag)
        self.out.mark_set("nm_ls", "end-1c")
        self.out.see("end")

    def system(self, text):
        self._insert_client_line(f"[NewMUD] {text}", "system")

    def echo_command(self, text):
        if self.line_text:
            # GMud style: command appears right after the prompt
            self.out.insert("end", text, "echo")
            self.line_triggered = True
            self.out.insert("end", "\n")
            self.finish_line()
            self.skip_newline = True
        else:
            self.out.insert("end", text + "\n", "echo")
            self.out.mark_set("nm_ls", "end-1c")
        if self.log_file:
            try:
                self.log_file.write(f"> {text}\n")
            except OSError:
                pass
        self.out.see("end")

    def _trim(self):
        limit = int(self.settings.get("scrollback_lines", 20000))
        lines = int(self.out.index("end-1c").split(".")[0])
        if lines > limit + 500:
            self.out.delete("1.0", f"{lines - limit}.0")

    def clear(self):
        self.out.delete("1.0", "end")
        self.line_text = ""
        self.out.mark_set("nm_ls", "end-1c")

    # ------------------------------------------------------------------ gauges
    def rebuild_gauges(self):
        for w in self.gauge_bar.winfo_children():
            w.destroy()
        self.gauge_widgets = {}
        self._gauge_regex = []
        s = self.settings
        for g in self.world.get("gauges", []):
            if not g.get("enabled", True) or not g.get("regex"):
                continue
            try:
                rx = re.compile(g["regex"])
                mx = re.compile(g["max_regex"]) if g.get("max_regex") else None
            except re.error:
                continue
            label = g.get("label", "?")
            self._gauge_regex.append((label, rx, mx))
            frame = tk.Frame(self.gauge_bar, bg=s["input_background"])
            frame.pack(side="left", padx=(8, 4), pady=3)
            tk.Label(frame, text=label, bg=s["input_background"], fg="#bbbbbb",
                     font=(s["font_family"], 9, "bold")).pack(side="left", padx=(0, 4))
            c = tk.Canvas(frame, width=150, height=14, bg="#2b2b2b", highlightthickness=0, bd=0)
            c.pack(side="left")
            self.gauge_widgets[label] = (c, g.get("color", "#16c60c"))
            self._draw_gauge(label)
        self.gauge_bar.pack_configure(fill="x")
        if not self.gauge_widgets:
            self.gauge_bar.configure(height=1)

    def check_gauges(self, line):
        changed = False
        for label, rx, mx in getattr(self, "_gauge_regex", []):
            m = rx.search(line)
            if not m:
                continue
            try:
                val = int(m.group(1) if m.groups() else m.group(0))
            except (ValueError, IndexError):
                continue
            self.gauge_values[label] = val
            if mx:
                m2 = mx.search(line)
                if m2:
                    try:
                        self.gauge_max[label] = int(m2.group(1) if m2.groups() else m2.group(0))
                    except ValueError:
                        pass
            else:
                self.gauge_max[label] = max(self.gauge_max.get(label, 0), val)
            changed = True
            self._draw_gauge(label)
        return changed

    def _draw_gauge(self, label):
        if label not in self.gauge_widgets:
            return
        c, color = self.gauge_widgets[label]
        c.delete("all")
        val = self.gauge_values.get(label)
        mx = self.gauge_max.get(label) or 0
        w, h = int(c.cget("width")), int(c.cget("height"))
        if val is None:
            c.create_text(w // 2, h // 2, text="—", fill="#777777", font=("Segoe UI", 8))
            return
        frac = max(0.0, min(1.0, val / mx)) if mx else 1.0
        c.create_rectangle(0, 0, int(w * frac), h, fill=color, width=0)
        c.create_text(w // 2, h // 2, text=f"{val:,} / {mx:,}" if mx else f"{val:,}",
                      fill="#ffffff", font=("Segoe UI", 8, "bold"))

    def reset_gauge_max(self):
        self.gauge_max.clear()
        for label in self.gauge_widgets:
            if label in self.gauge_values:
                self.gauge_max[label] = self.gauge_values[label]
            self._draw_gauge(label)

    # ------------------------------------------------------------------ timers
    def start_timers(self):
        for job in self.timer_jobs:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self.timer_jobs = []
        for t in self.world.get("timers", []):
            if t.get("enabled", True) and t.get("command") and float(t.get("interval", 0) or 0) >= 1:
                self._schedule_timer(t)

    def _schedule_timer(self, t):
        ms = int(float(t["interval"]) * 1000)

        def fire():
            if t not in self.world.get("timers", []) or not t.get("enabled", True):
                return
            if self.connected:
                self.send_command(t["command"], echo=True, from_trigger=True)
            self.timer_jobs.append(self.after(ms, fire))
        self.timer_jobs.append(self.after(ms, fire))

    # ------------------------------------------------------------------ input
    def _on_enter(self, _=None):
        text = self.input.get()
        if self.password_mode:
            self.input.delete(0, "end")
            if self.conn and self.conn.alive:
                self.conn.send_line(text)
            return "break"
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            del self.history[:-500]
        self.hist_pos = len(self.history)
        self.send_command(text, echo=True)
        if self.settings.get("keep_input"):
            self.input.select_range(0, "end")
            self.input.icursor("end")
        else:
            self.input.delete(0, "end")
        self.close_split()
        return "break"

    def send_command(self, text, echo=True, from_trigger=False):
        actions = self.auto.process_input(text)
        sent_any = False
        for act in actions:
            if act[0] == "send":
                if echo and self.settings.get("local_echo", True):
                    self.echo_command(act[1])
                if self.conn and self.conn.alive:
                    self.conn.send_line(act[1])
                    sent_any = True
                elif not from_trigger:
                    self.system("Not connected. Use #connect or File > Worlds.")
                    break
            else:
                self.client_command(act[1], act[2])
        return sent_any

    def client_command(self, name, args):
        if name == "echo":
            self._insert_client_line(args, "system")
        elif name == "error":
            self.system(args)
        elif name == "connect":
            self.connect()
        elif name in ("disconnect", "dc", "zap"):
            self.disconnect()
        elif name == "reconnect":
            self.reconnect()
        elif name == "log":
            self.toggle_log()
        elif name in ("clear", "cls"):
            self.clear()
        elif name == "gag":
            self.gag_enabled = True
            self.system("Gag triggers are active.")
        elif name == "nogag":
            self.gag_enabled = False
            self.system("Gag triggers are paused - gagged lines will show.")
        elif name == "help":
            for line in HELP_TEXT.splitlines():
                self._insert_client_line(line, "system")
        else:
            self.system(f"Unknown client command '{name}'. Type {self.auto.cmd_char}help for help.")

    def _history(self, step):
        if not self.history:
            return "break"
        if self.hist_pos == len(self.history):
            self.hist_draft = self.input.get()
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        text = self.hist_draft if self.hist_pos == len(self.history) else self.history[self.hist_pos]
        self.input.delete(0, "end")
        self.input.insert(0, text)
        return "break"

    def _history_search(self, step):
        prefix = self.input.get()[:self.input.index("insert")]
        i = self.hist_pos + step
        while 0 <= i < len(self.history):
            if self.history[i].startswith(prefix) and self.history[i] != self.input.get():
                self.hist_pos = i
                self.input.delete(0, "end")
                self.input.insert(0, self.history[i])
                self.input.icursor(len(prefix))
                return "break"
            i += step
        self.bell()
        return "break"

    def _complete(self, _):
        text = self.input.get()
        cur = self.input.index("insert")
        if self.complete_state and self.complete_state["text"] == text:
            st = self.complete_state
            st["i"] = (st["i"] + 1) % len(st["options"])
        else:
            m = re.search(r"(\w+)$", text[:cur])
            if not m:
                return "break"
            prefix = m.group(1)
            recent = self.out.get("end-300l", "end")
            words, seen = [], set()
            for w in reversed(re.findall(r"[A-Za-z][\w'-]{2,}", recent)):
                lw = w.lower()
                if lw.startswith(prefix.lower()) and lw != prefix.lower() and lw not in seen:
                    seen.add(lw)
                    words.append(w)
            if not words:
                self.bell()
                return "break"
            st = self.complete_state = {"start": cur - len(prefix), "end": cur, "options": words, "i": 0,
                                        "prefix": prefix}
        word = st["options"][st["i"]]
        if st["prefix"].islower():
            word = word.lower()
        new = text[:st["start"]] + word + text[st["end"]:]
        st["end"] = st["start"] + len(word)
        self.input.delete(0, "end")
        self.input.insert(0, new)
        self.input.icursor(st["end"])
        st["text"] = new
        return "break"

    def _on_escape(self, _):
        if self.split_open:
            self.close_split()
        else:
            self.input.delete(0, "end")
            self.hist_pos = len(self.history)
        return "break"

    @staticmethod
    def key_name(event):
        """Normalise a key event to names like 'F1', 'Shift+F5', 'Ctrl+F2', 'Num8'."""
        if sys.platform == "win32" and event.keycode in NUMPAD_KEYCODES and event.keysym not in (
                "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End", "Insert", "Delete"):
            # keypad digits with NumLock on report as plain digits; keycode tells them apart
            if event.keysym.isdigit() or event.keysym in ("asterisk", "plus", "minus", "slash", "period",
                                                          "decimal", "KP_Decimal") or event.keysym.startswith("KP_"):
                return NUMPAD_KEYCODES[event.keycode]
        if event.keysym in NUMPAD_KEYSYMS:
            return NUMPAD_KEYSYMS[event.keysym]
        if re.fullmatch(r"F\d{1,2}", event.keysym):
            mods = []
            if event.state & 0x4:
                mods.append("Ctrl")
            if event.state & 0x20000 or event.state & 0x8:
                mods.append("Alt")
            if event.state & 0x1:
                mods.append("Shift")
            return "+".join(mods + [event.keysym])
        return None

    def _input_key(self, event):
        self.complete_state = None if event.keysym != "Tab" else self.complete_state
        name = self.key_name(event)
        if not name:
            return None
        for m in self.world.get("macros", []):
            if m.get("enabled", True) and m.get("key", "").lower() == name.lower() and m.get("command"):
                self.send_command(m["command"], echo=True)
                return "break"
        if name.startswith("Num") and self.settings.get("numpad_walking", True) and name in NUMPAD_WALK:
            self.send_command(NUMPAD_WALK[name], echo=True)
            return "break"
        return None

    def _output_key(self, event):
        if event.state & 0x4:  # Ctrl: allow copy, never edit the output
            key = event.keysym.lower()
            if key == "f":
                self.app.find()
            return None if key in ("c", "a") else "break"
        self.input.focus_set()
        handled = self._input_key(event)
        if handled:
            return handled
        if event.char and event.char.isprintable():
            self.input.insert("insert", event.char)
        elif event.keysym in ("Return", "KP_Enter"):
            self._on_enter()
        return "break"

    def _copy_selection(self, event):
        w = event.widget
        try:
            text = w.get("sel.first", "sel.last")
        except tk.TclError:
            text = ""
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.flash_status(f"Copied {len(text)} characters")
        self.after(10, self.input.focus_set)

    def _open_link(self, event):
        w = event.widget
        idx = w.index(f"@{event.x},{event.y}")
        rng = w.tag_prevrange("link", idx + "+1c")
        if rng:
            url = w.get(*rng)
            if not url.lower().startswith("http"):
                url = "http://" + url
            webbrowser.open(url)

    # -------------------------------------------------------------- scrollback
    def open_split(self):
        if self.split_open:
            return
        self.split_open = True
        self.pane.add(self.back_frame, before=self.pane.panes()[0], stretch="always")
        self.update_idletasks()
        total = self.pane.winfo_height()
        try:
            self.pane.sash_place(0, 0, int(total * 0.65))
        except tk.TclError:
            pass
        self.back.yview_moveto(1.0)

    def close_split(self):
        if not self.split_open:
            return "break"
        self.split_open = False
        self.pane.forget(self.back_frame)
        self.out.see("end")
        self.out.tag_remove("found", "1.0", "end")
        return "break"

    def _scroll_back(self, units):
        if units < 0 and not self.split_open:
            self.open_split()
        if self.split_open:
            self.back.yview_scroll(units, "units")
        return "break"

    def _on_wheel(self, event):
        return self._scroll_back(-3 if event.delta > 0 else 3)

    def _page(self, direction):
        if direction < 0 and not self.split_open:
            self.open_split()
        if self.split_open:
            self.back.yview_scroll(direction, "pages")
        return "break"

    def _back_scrolled(self, first, last):
        if self.split_open and float(last) >= 0.9999 and getattr(self, "_split_armed", False):
            self.after_idle(self.close_split)
        self._split_armed = float(last) < 0.999

    # ------------------------------------------------------------------ search
    def find_text(self, pattern, backwards=True, regex=False, nocase=True):
        self.open_split()
        w = self.back
        self.out.tag_remove("found", "1.0", "end")
        start = getattr(self, "_find_pos", None) or ("end" if backwards else "1.0")
        count = tk.IntVar()
        idx = w.search(pattern, start, stopindex="1.0" if backwards else "end", backwards=backwards,
                       regexp=regex, nocase=nocase, count=count)
        if not idx:
            self._find_pos = None
            return False
        end = f"{idx}+{count.get()}c"
        self.out.tag_add("found", idx, end)
        self.out.tag_raise("found")
        w.see(idx)
        self._split_armed = False
        self._find_pos = idx if backwards else end
        return True

    # ------------------------------------------------------------------ logging
    def toggle_log(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None
            self.system("Logging stopped.")
        else:
            folder = self.settings.get("log_folder") or os.path.join(app_dir(), "logs")
            os.makedirs(folder, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", self.world["name"])
            path = os.path.join(folder, f"{safe}_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.txt")
            self.log_file = open(path, "a", encoding="utf-8", buffering=1)
            self.system(f"Logging to {path}")
        self.app.update_status()

    def export_html(self, path):
        s = self.settings
        out = [f"<!doctype html><meta charset='utf-8'><title>{html.escape(self.world['name'])}</title>",
               f"<body style='background:{s['background']};color:{s['foreground']};"
               f"font-family:{s['font_family']},monospace;font-size:{s['font_size']}pt'><pre style='white-space:pre-wrap'>"]
        styles = {}
        for tag in self.out.tag_names():
            fg = self.out.tag_cget(tag, "foreground")
            bg = self.out.tag_cget(tag, "background")
            css = (f"color:{fg};" if fg else "") + (f"background:{bg};" if bg else "")
            if self.out.tag_cget(tag, "underline") not in ("", "0", 0, False):
                css += "text-decoration:underline;"
            if css and tag not in ("sel", "found"):
                styles[tag] = css
        active = []
        for key, value, _ in self.out.dump("1.0", "end-1c", text=True, tag=True):
            if key == "tagon" and value in styles:
                active.append(value)
            elif key == "tagoff" and value in active:
                active.remove(value)
            elif key == "text":
                css = "".join(styles[t] for t in active)
                txt = html.escape(value)
                out.append(f"<span style='{css}'>{txt}</span>" if css else txt)
        out.append("</pre></body>")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(out))

    def destroy_session(self):
        self.disconnect()
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        for job in self.timer_jobs:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass


class NewMudApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.store = Store()
        self.title(f"NewMUD {__version__}")
        self.geometry(self.store.settings.get("geometry", "1100x720"))
        self.minsize(500, 300)
        self.configure(bg="#1e1e1e")
        try:
            icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "newmud.ico")
            if os.path.exists(icon):
                self.iconbitmap(icon)
        except tk.TclError:
            pass
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.sessions = []
        self._build_menu()
        self.status = tk.Label(self, anchor="w", bg="#007acc", fg="white", padx=8, font=("Segoe UI", 9))
        self.status.pack(fill="x", side="bottom")
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.notebook.bind("<Button-2>", self._middle_close)
        self._flash_job = None
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.bind_all("<Control-f>", lambda e: self.find())
        self.bind_all("<Control-w>", lambda e: self.close_tab())
        self.bind_all("<Control-Tab>", lambda e: self._cycle_tab(1))
        self.bind_all("<Control-Shift-Tab>", lambda e: self._cycle_tab(-1))
        self.update_status()
        self.after(15, self._loop)
        self.after(1000, self._tick)
        self.after(100, self._startup)

    # ----------------------------------------------------------------- startup
    def _startup(self):
        args = sys.argv[1:]
        if len(args) >= 2 and args[1].isdigit():
            w = new_world(f"{args[0]}:{args[1]}", args[0], int(args[1]))
            self.open_world(w)
        elif args:
            w = self.store.find_world(args[0])
            if w:
                self.open_world(w)
        if not self.sessions:
            self._welcome()

    def _welcome(self):
        gmud = None
        for cand in (os.path.join(app_dir(), "WorldLis.mud"), os.path.join(os.path.dirname(app_dir()), "WorldLis.mud")):
            if os.path.exists(cand):
                gmud = cand
                break
        if gmud and not self.store.settings.get("gmud_import_offered"):
            self.store.settings["gmud_import_offered"] = True
            self.store.save()
            if messagebox.askyesno("NewMUD", "Found a GMud world list next to NewMUD.\n\n"
                                             "Import your GMud worlds, triggers and macros?"):
                self.import_gmud(path=gmud)
        WorldManager(self)

    # -------------------------------------------------------------------- menu
    def _build_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Worlds…", accelerator="Ctrl+O", command=lambda: WorldManager(self))
        f.add_command(label="Quick connect…", command=self.quick_connect)
        f.add_separator()
        f.add_command(label="Connect", command=lambda: self._with_tab(lambda t: t.connect()))
        f.add_command(label="Disconnect", command=lambda: self._with_tab(lambda t: t.disconnect()))
        f.add_command(label="Reconnect", command=lambda: self._with_tab(lambda t: t.reconnect()))
        f.add_command(label="Close tab", accelerator="Ctrl+W", command=self.close_tab)
        f.add_separator()
        f.add_command(label="Start / stop log", accelerator="#log", command=lambda: self._with_tab(lambda t: t.toggle_log()))
        f.add_command(label="Save output as HTML…", command=self.save_html)
        f.add_separator()
        f.add_command(label="Import from GMud…", command=self.import_gmud)
        f.add_separator()
        f.add_command(label="Exit", command=self.quit_app)
        m.add_cascade(label="File", menu=f)
        self.bind_all("<Control-o>", lambda e: WorldManager(self))

        e = tk.Menu(m, tearoff=0)
        e.add_command(label="Find in scrollback…", accelerator="Ctrl+F", command=self.find)
        e.add_command(label="Clear screen", accelerator="#clear", command=lambda: self._with_tab(lambda t: t.clear()))
        e.add_command(label="Reset gauge maximums", command=lambda: self._with_tab(lambda t: t.reset_gauge_max()))
        m.add_cascade(label="Edit", menu=e)

        w = tk.Menu(m, tearoff=0)
        w.add_command(label="Aliases…", command=self.edit_aliases)
        w.add_command(label="Triggers…", command=self.edit_triggers)
        w.add_command(label="Macros (keys)…", command=self.edit_macros)
        w.add_command(label="Timers…", command=self.edit_timers)
        w.add_command(label="Gauges…", command=self.edit_gauges)
        m.add_cascade(label="World", menu=w)

        o = tk.Menu(m, tearoff=0)
        o.add_command(label="Settings…", command=lambda: SettingsDialog(self))
        o.add_separator()
        self._opt_vars = {}
        for key, label in (("local_echo", "Local echo"), ("keep_input", "Keep input after sending"),
                           ("numpad_walking", "Numpad walking"), ("auto_reconnect", "Auto reconnect")):
            var = tk.BooleanVar(value=self.store.settings.get(key))
            self._opt_vars[key] = var
            o.add_checkbutton(label=label, variable=var, command=lambda k=key: self._toggle_opt(k))
        m.add_cascade(label="Options", menu=o)

        h = tk.Menu(m, tearoff=0)
        h.add_command(label="Quick guide", accelerator="F1 in #help", command=self.show_help)
        h.add_command(label="About NewMUD", command=self.about)
        m.add_cascade(label="Help", menu=h)
        self.config(menu=m)

    def _toggle_opt(self, key):
        self.store.settings[key] = bool(self._opt_vars[key].get())
        self.store.save()

    # ---------------------------------------------------------------- sessions
    def open_world(self, world):
        tab = SessionTab(self, world)
        self.sessions.append(tab)
        self.notebook.add(tab, text=f" {world['name']} ")
        self.notebook.select(tab)
        tab.input.focus_set()
        tab.connect()
        return tab

    def current_tab(self):
        try:
            sel = self.notebook.select()
        except tk.TclError:
            return None
        for t in self.sessions:
            if str(t) == sel:
                return t
        return None

    def _with_tab(self, fn):
        t = self.current_tab()
        if t:
            fn(t)
        else:
            WorldManager(self)

    def close_tab(self, tab=None):
        tab = tab or self.current_tab()
        if not tab:
            return
        if tab.connected and not messagebox.askyesno("NewMUD", f"Disconnect from {tab.world['name']} and close the tab?"):
            return
        tab.destroy_session()
        self.sessions.remove(tab)
        self.notebook.forget(tab)
        tab.destroy()
        self.store.save()
        self.update_status()

    def _middle_close(self, event):
        try:
            idx = self.notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        self.close_tab(self.sessions[idx])

    def _cycle_tab(self, step):
        if self.sessions:
            i = (self.notebook.index("current") + step) % len(self.sessions)
            self.notebook.select(i)
        return "break"

    def mark_tab(self, tab, alert=False):
        try:
            prefix = "● " if alert else "• "
            if alert or not self.notebook.tab(tab, "text").startswith("●"):
                self.notebook.tab(tab, text=f"{prefix}{tab.world['name']} ")
        except tk.TclError:
            pass

    def _tab_changed(self, _):
        t = self.current_tab()
        if t:
            t.unseen = False
            self.notebook.tab(t, text=f" {t.world['name']} ")
            t.input.focus_set()
        self.update_status()

    def _loop(self):
        for t in list(self.sessions):
            t.poll()
        self.after(15, self._loop)

    def _tick(self):
        self.update_status()
        self.after(1000, self._tick)

    # ------------------------------------------------------------------ status
    def update_status(self):
        if self._flash_job:
            return
        t = self.current_tab()
        if not t:
            self.status.configure(text="Not connected - File > Worlds (Ctrl+O) to pick a MUD", bg="#555555")
            return
        if t.connected:
            secs = int(time.time() - (t.connected_at or time.time()))
            parts = [f"Connected to {t.world['host']}:{t.world['port']}",
                     f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}",
                     f"{t.conn.bytes_in / 1024:,.0f} KB received"]
            if t.conn.telnet.compressed:
                parts.append("compressed (MCCP)")
            bg = "#007acc"
        else:
            parts = ["Disconnected - type #connect"]
            bg = "#a1260d"
        if t.log_file:
            parts.append("● logging")
        if t.password_mode:
            parts.append("password entry")
        self.status.configure(text="   |   ".join(parts), bg=bg)

    def flash_status(self, text):
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self.status.configure(text=text, bg="#3a7d44")

        def done():
            self._flash_job = None
            self.update_status()
        self._flash_job = self.after(1200, done)

    # ----------------------------------------------------------------- editors
    def _world_for_edit(self):
        t = self.current_tab()
        if t:
            return t.world, t
        if self.store.worlds:
            return self.store.worlds[0], None
        messagebox.showinfo("NewMUD", "Create a world first (File > Worlds).")
        return None, None

    def _changed(self, tab):
        def cb():
            if tab:
                tab.auto.reload()
                tab.start_timers()
                tab.rebuild_gauges()
            self.store.save()
        return cb

    def edit_aliases(self):
        world, tab = self._world_for_edit()
        if world is None:
            return
        ListEditor(self, f"Aliases - {world['name']}", world.setdefault("aliases", []),
                   [("name", "Alias", "str", None), ("replacement", "Replace with", "str", None),
                    ("enabled", "Enabled", "bool", None)],
                   self._changed(tab), defaults={"enabled": True},
                   help_text="Typing the alias word runs the replacement. %1..%9 are the words you type after it, "
                             "%* is all of them; without any %, the words are appended. Use ; for several commands. "
                             "Start the alias with ^ to use a regular expression.  Example:  k  →  kill %1")

    def edit_triggers(self):
        world, tab = self._world_for_edit()
        if world is None:
            return
        ListEditor(self, f"Triggers - {world['name']}", world.setdefault("triggers", []),
                   [("pattern", "Pattern", "str", None),
                    ("type", "Match type", "choice", ["contains", "wildcard", "regex", "exact"]),
                    ("send", "Send", "str", None), ("highlight", "Highlight color", "color", None),
                    ("gag", "Hide line (gag)", "bool", None), ("sound", "Play sound", "file", None),
                    ("enabled", "Enabled", "bool", None)],
                   self._changed(tab), columns=["pattern", "send", "enabled"],
                   defaults={"type": "contains", "enabled": True},
                   help_text="When a line from the MUD matches, NewMUD can send commands, color the match, hide the "
                             "line or play a sound. Wildcard: * matches anything, ? one word, captured as %1, %2… "
                             "Example:  * tells you '*'   →   send:  #echo %1 said %2")

    def edit_macros(self):
        world, tab = self._world_for_edit()
        if world is None:
            return
        ListEditor(self, f"Macros - {world['name']}", world.setdefault("macros", []),
                   [("key", "Key", "str", None), ("command", "Command", "str", None),
                    ("enabled", "Enabled", "bool", None)],
                   self._changed(tab), defaults={"enabled": True},
                   help_text="Key names: F1…F12, Shift+F1, Ctrl+F1, Alt+F1, Num0…Num9, Num+, Num-, Num*, Num/. "
                             "Keypad macros override the built-in numpad walking.")

    def edit_timers(self):
        world, tab = self._world_for_edit()
        if world is None:
            return
        ListEditor(self, f"Timers - {world['name']}", world.setdefault("timers", []),
                   [("name", "Name", "str", None), ("interval", "Every (seconds)", "int", None),
                    ("command", "Command", "str", None), ("enabled", "Enabled", "bool", None)],
                   self._changed(tab), columns=["name", "interval", "command"],
                   defaults={"interval": 60, "enabled": True},
                   help_text="Timers send a command repeatedly while you are connected. "
                             "Please respect your MUD's rules about automation.")

    def edit_gauges(self):
        world, tab = self._world_for_edit()
        if world is None:
            return
        if not world.get("gauges"):
            import copy
            world["gauges"] = copy.deepcopy(DEFAULT_GAUGES)
        ListEditor(self, f"Gauges - {world['name']}", world["gauges"],
                   [("label", "Label", "str", None), ("regex", "Value regex", "str", None),
                    ("max_regex", "Max regex (optional)", "str", None), ("color", "Color", "color", None),
                    ("enabled", "Enabled", "bool", None)],
                   self._changed(tab), columns=["label", "regex", "enabled"],
                   defaults={"enabled": True, "color": "#16c60c"},
                   help_text="Gauges read numbers from your prompt with a regular expression; the first (group) is "
                             "the value. Example prompt '5249H 126031M 62975V' → (\\d+)H. Without a max regex the bar "
                             "uses the highest value seen (Edit > Reset gauge maximums after a level up).")

    # ------------------------------------------------------------------- misc
    def apply_settings(self):
        for t in self.sessions:
            t.apply_settings()
        for k, var in self._opt_vars.items():
            var.set(self.store.settings.get(k))

    def quick_connect(self):
        s = simpledialog.askstring("Quick connect", "Host and port (e.g.  4dimensions.org 6000):", parent=self)
        if not s:
            return
        parts = s.replace(":", " ").split()
        if len(parts) != 2 or not parts[1].isdigit():
            messagebox.showerror("NewMUD", "Please enter a host and a port, like:  mud.example.com 4000")
            return
        self.open_world(new_world(f"{parts[0]}:{parts[1]}", parts[0], int(parts[1])))

    def import_gmud(self, path=None, parent=None):
        path = path or filedialog.askopenfilename(parent=parent or self, title="Find GMud's WorldLis.mud",
                                                  filetypes=(("GMud world list", "WorldLis.mud"), ("All files", "*.*")))
        if not path:
            return
        try:
            worlds = import_worldlist(path)
        except Exception as e:  # noqa: BLE001 - show any parse problem to the user
            messagebox.showerror("NewMUD", f"Could not read that file:\n{e}", parent=parent or self)
            return
        if not worlds:
            messagebox.showinfo("NewMUD", "No worlds found in that file.", parent=parent or self)
            return
        names = []
        for w in worlds:
            existing = self.store.find_world(w["name"])
            if existing and existing["host"] == w["host"]:
                existing["triggers"].extend(t for t in w["triggers"] if t not in existing["triggers"])
                existing["macros"] = w["macros"]
            else:
                self.store.worlds.append(w)
            names.append(w["name"])
        self.store.save()
        messagebox.showinfo("NewMUD", "Imported from GMud: " + ", ".join(names), parent=parent or self)

    def find(self):
        t = self.current_tab()
        if not t:
            return "break"
        if getattr(self, "_find_win", None) and self._find_win.winfo_exists():
            self._find_win.lift()
            self._find_entry.focus_set()
            return "break"
        win = self._find_win = tk.Toplevel(self)
        win.title("Find")
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=8)
        f.pack()
        e = self._find_entry = ttk.Entry(f, width=32)
        e.grid(row=0, column=0, columnspan=3, padx=2, pady=2)
        rx = tk.BooleanVar()
        ttk.Checkbutton(f, text="Regex", variable=rx).grid(row=1, column=0, sticky="w")
        status = ttk.Label(f, text="")
        status.grid(row=2, column=0, columnspan=3, sticky="w")

        def go(back):
            tab = self.current_tab()
            if tab and e.get():
                ok = tab.find_text(e.get(), backwards=back, regex=rx.get())
                status.configure(text="" if ok else "No more matches.")
        ttk.Button(f, text="▲ Older", command=lambda: go(True)).grid(row=1, column=1)
        ttk.Button(f, text="▼ Newer", command=lambda: go(False)).grid(row=1, column=2)
        e.bind("<Return>", lambda ev: go(True))
        e.bind("<Shift-Return>", lambda ev: go(False))
        win.bind("<Escape>", lambda ev: win.destroy())
        t._find_pos = None
        center(win, self)
        e.focus_set()
        return "break"

    def save_html(self):
        t = self.current_tab()
        if not t:
            return
        path = filedialog.asksaveasfilename(defaultextension=".html", filetypes=(("Web page", "*.html"),),
                                            initialfile=f"{t.world['name']}_{datetime.datetime.now():%Y-%m-%d}.html")
        if path:
            t.export_html(path)
            self.flash_status(f"Saved {os.path.basename(path)}")

    def show_help(self):
        win = tk.Toplevel(self)
        win.title("NewMUD quick guide")
        txt = tk.Text(win, width=86, height=40, font=("Consolas", 10), bg="#1e1e1e", fg="#dddddd", padx=10, pady=8)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        center(win, self)

    def about(self):
        messagebox.showinfo("About NewMUD",
                            f"NewMUD {__version__}\n\nA free, open-source MUD client in the spirit of the classic "
                            "GMud, rebuilt for today's MUDs: full 256 and true color, tabs, gauges, triggers, "
                            "MCCP compression and more.\n\nMIT License.")

    def quit_app(self):
        if any(t.connected for t in self.sessions):
            if not messagebox.askyesno("NewMUD", "You are still connected. Quit anyway?"):
                return
        for t in self.sessions:
            t.destroy_session()
        try:
            self.store.settings["geometry"] = self.geometry()
            self.store.save()
        except OSError:
            pass
        self.destroy()


def main():
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens instead of blurry scaling
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    app = NewMudApp()
    app.mainloop()
