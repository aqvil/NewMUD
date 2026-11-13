"""Dialog windows: world manager, list editors, settings."""
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser, filedialog, messagebox, ttk

from .storage import new_world

PAD = {"padx": 6, "pady": 3}


def center(win, parent):
    win.update_idletasks()
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except tk.TclError:
        pass


class ColorField(ttk.Frame):
    def __init__(self, master, allow_empty=True):
        super().__init__(master)
        self.var = tk.StringVar()
        self.swatch = tk.Label(self, width=3, relief="sunken")
        self.swatch.pack(side="left")
        ttk.Entry(self, textvariable=self.var, width=10).pack(side="left", padx=4)
        ttk.Button(self, text="Pick…", width=6, command=self.pick).pack(side="left")
        if allow_empty:
            ttk.Button(self, text="None", width=5, command=lambda: self.var.set("")).pack(side="left", padx=(4, 0))
        self.var.trace_add("write", lambda *a: self._update())

    def _update(self):
        try:
            self.swatch.configure(bg=self.var.get() or self.master.cget("background"))
        except tk.TclError:
            self.swatch.configure(bg="#808080")

    def pick(self):
        _, hexcolor = colorchooser.askcolor(self.var.get() or "#ffffff", parent=self)
        if hexcolor:
            self.var.set(hexcolor)

    def get(self):
        return self.var.get().strip()

    def set(self, v):
        self.var.set(v or "")


class FileField(ttk.Frame):
    def __init__(self, master, filetypes=(("Sound files", "*.wav"), ("All files", "*.*"))):
        super().__init__(master)
        self.var = tk.StringVar()
        self.filetypes = filetypes
        ttk.Entry(self, textvariable=self.var, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(self, text="Browse…", command=self.browse).pack(side="left", padx=4)

    def browse(self):
        path = filedialog.askopenfilename(parent=self, filetypes=self.filetypes)
        if path:
            self.var.set(path)

    def get(self):
        return self.var.get().strip()

    def set(self, v):
        self.var.set(v or "")


class ListEditor(tk.Toplevel):
    """Generic editor for a list of dicts (aliases, triggers, macros, timers, gauges).

    fields: list of (key, label, kind, extra)
      kind: "str", "int", "bool", "choice" (extra = options), "color", "file", "text"
    """

    def __init__(self, parent, title, items, fields, on_change, help_text="", columns=None, defaults=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.items = items
        self.fields = fields
        self.on_change = on_change
        self.defaults = defaults or {}
        self.current = None
        self.widgets = {}
        cols = columns or [f[0] for f in fields[:3]]
        self.columns = cols

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        if help_text:
            ttk.Label(body, text=help_text, wraplength=560, foreground="#555").pack(anchor="w", pady=(0, 6))

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=10, selectmode="browse")
        labels = {f[0]: f[1] for f in fields}
        for c in cols:
            self.tree.heading(c, text=labels.get(c, c))
            self.tree.column(c, width=180 if c != "enabled" else 60, stretch=c != "enabled")
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.LabelFrame(body, text="Details", padding=6)
        form.pack(fill="x", pady=8)
        for row, (key, label, kind, extra) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="nw", **PAD)
            if kind == "bool":
                var = tk.BooleanVar()
                w = ttk.Checkbutton(form, variable=var)
                w.var = var
            elif kind == "choice":
                w = ttk.Combobox(form, values=extra, state="readonly", width=20)
            elif kind == "color":
                w = ColorField(form)
            elif kind == "file":
                w = FileField(form)
            elif kind == "text":
                w = tk.Text(form, height=3, width=50, font=("Consolas", 10))
            else:
                w = ttk.Entry(form, width=52)
            w.grid(row=row, column=1, sticky="we", **PAD)
            self.widgets[key] = (w, kind)
        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(body)
        btns.pack(fill="x")
        ttk.Button(btns, text="New", command=self.new).pack(side="left")
        ttk.Button(btns, text="Save", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete", command=self.delete).pack(side="left")
        ttk.Button(btns, text="▲", width=3, command=lambda: self.move(-1)).pack(side="left", padx=(12, 2))
        ttk.Button(btns, text="▼", width=3, command=lambda: self.move(1)).pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        self.refresh()
        if self.items:
            self.tree.selection_set("0")
        else:
            self.new()
        self.geometry("640x560")
        center(self, parent)
        self.bind("<Escape>", lambda e: self.destroy())

    def _value(self, item, key):
        v = item.get(key, "")
        if isinstance(v, bool):
            return "✔" if v else ""
        return str(v).replace("\n", " ⏎ ")

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=[self._value(item, c) for c in self.columns])

    def _on_select(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        self.current = int(sel[0])
        self._fill(self.items[self.current])

    def _fill(self, item):
        for key, (w, kind) in self.widgets.items():
            v = item.get(key, self.defaults.get(key, "" if kind != "bool" else False))
            if kind == "bool":
                w.var.set(bool(v))
            elif kind == "choice":
                w.set(v or (w.cget("values")[0] if w.cget("values") else ""))
            elif kind in ("color", "file"):
                w.set(v)
            elif kind == "text":
                w.delete("1.0", "end")
                w.insert("1.0", v)
            else:
                w.delete(0, "end")
                w.insert(0, str(v))

    def _collect(self):
        out = {}
        for key, (w, kind) in self.widgets.items():
            if kind == "bool":
                out[key] = bool(w.var.get())
            elif kind == "text":
                out[key] = w.get("1.0", "end-1c")
            elif kind == "int":
                try:
                    out[key] = int(float(w.get()))
                except ValueError:
                    out[key] = self.defaults.get(key, 0)
            else:
                out[key] = w.get()
        return out

    def new(self):
        self.tree.selection_remove(self.tree.selection())
        self.current = None
        self._fill(dict(self.defaults))

    def save(self):
        data = self._collect()
        first = self.fields[0][0]
        if not str(data.get(first, "")).strip():
            messagebox.showwarning("NewMUD", f"'{self.fields[0][1]}' cannot be empty.", parent=self)
            return
        if self.current is None:
            self.items.append(data)
            self.current = len(self.items) - 1
        else:
            self.items[self.current].update(data)
        self.refresh()
        self.tree.selection_set(str(self.current))
        self.on_change()

    def delete(self):
        if self.current is None:
            return
        del self.items[self.current]
        self.current = None
        self.refresh()
        self.new()
        self.on_change()

    def move(self, delta):
        if self.current is None:
            return
        j = self.current + delta
        if 0 <= j < len(self.items):
            self.items[self.current], self.items[j] = self.items[j], self.items[self.current]
            self.current = j
            self.refresh()
            self.tree.selection_set(str(j))
            self.on_change()


class WorldDialog(tk.Toplevel):
    """Edit one world's connection details."""

    def __init__(self, parent, world, on_ok):
        super().__init__(parent)
        self.title(f"World - {world.get('name') or 'New'}")
        self.transient(parent)
        self.grab_set()
        self.world = world
        self.on_ok = on_ok
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        self.entries = {}
        for row, (key, label) in enumerate([("name", "Name"), ("host", "Host"), ("port", "Port"),
                                            ("description", "Description")]):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", **PAD)
            e = ttk.Entry(f, width=40)
            e.insert(0, str(world.get(key, "")))
            e.grid(row=row, column=1, sticky="we", **PAD)
            self.entries[key] = e
        ttk.Label(f, text="Encoding").grid(row=4, column=0, sticky="w", **PAD)
        self.enc = ttk.Combobox(f, values=["auto", "utf-8", "cp1252", "latin-1", "cp437"], width=12)
        self.enc.set(world.get("encoding", "auto"))
        self.enc.grid(row=4, column=1, sticky="w", **PAD)
        ttk.Label(f, text="Send on connect").grid(row=5, column=0, sticky="nw", **PAD)
        self.connect_cmds = tk.Text(f, height=4, width=40, font=("Consolas", 10))
        self.connect_cmds.insert("1.0", world.get("connect_commands", ""))
        self.connect_cmds.grid(row=5, column=1, sticky="we", **PAD)
        ttk.Label(f, text="One command per line (e.g. your character name).\n"
                          "Stored as plain text - avoid putting passwords here.",
                  foreground="#777").grid(row=6, column=1, sticky="w", padx=6)
        b = ttk.Frame(f)
        b.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="OK", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="left")
        f.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda e: self.ok() if e.widget is not self.connect_cmds else None)
        self.bind("<Escape>", lambda e: self.destroy())
        center(self, parent)
        self.entries["name"].focus_set()

    def ok(self):
        name = self.entries["name"].get().strip()
        host = self.entries["host"].get().strip()
        try:
            port = int(self.entries["port"].get().strip())
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            messagebox.showerror("NewMUD", "Port must be a number between 1 and 65535.", parent=self)
            return
        if not name or not host:
            messagebox.showerror("NewMUD", "Name and host are required.", parent=self)
            return
        self.world.update(name=name, host=host, port=port,
                          description=self.entries["description"].get().strip(),
                          encoding=self.enc.get().strip() or "auto",
                          connect_commands=self.connect_cmds.get("1.0", "end-1c"))
        self.destroy()
        self.on_ok(self.world)


class WorldManager(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Worlds")
        self.transient(app)
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(f, columns=("name", "host", "port", "description"), show="headings", height=12)
        for c, w in (("name", 140), ("host", 170), ("port", 60), ("description", 200)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, stretch=c == "description")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.connect())
        self.tree.bind("<Return>", lambda e: self.connect())
        b = ttk.Frame(f, padding=(10, 0, 0, 0))
        b.pack(side="left", fill="y")
        for text, cmd in (("Connect", self.connect), ("New…", self.new), ("Edit…", self.edit),
                          ("Copy", self.copy), ("Delete", self.delete), (None, None),
                          ("Import GMud…", self.import_gmud)):
            if text is None:
                ttk.Separator(b).pack(fill="x", pady=8)
                continue
            ttk.Button(b, text=text, width=16, command=cmd).pack(pady=2)
        ttk.Button(b, text="Close", width=16, command=self.destroy).pack(side="bottom")
        self.refresh()
        center(self, app)
        self.bind("<Escape>", lambda e: self.destroy())
        self.tree.focus_set()

    def refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, w in enumerate(self.app.store.worlds):
            self.tree.insert("", "end", iid=str(i), values=(w["name"], w["host"], w["port"], w.get("description", "")))
        if self.app.store.worlds:
            idx = select if select is not None else 0
            self.tree.selection_set(str(min(idx, len(self.app.store.worlds) - 1)))
            self.tree.focus(str(min(idx, len(self.app.store.worlds) - 1)))

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def connect(self):
        i = self._selected()
        if i is None:
            return
        self.destroy()
        self.app.open_world(self.app.store.worlds[i])

    def new(self):
        w = new_world("", "", 23)

        def done(world):
            self.app.store.worlds.append(world)
            self.app.store.save()
            self.refresh(len(self.app.store.worlds) - 1)
        WorldDialog(self, w, done)

    def edit(self):
        i = self._selected()
        if i is None:
            return

        def done(world):
            self.app.store.save()
            self.refresh(i)
        WorldDialog(self, self.app.store.worlds[i], done)

    def copy(self):
        import copy
        i = self._selected()
        if i is None:
            return
        w = copy.deepcopy(self.app.store.worlds[i])
        w["name"] += " (copy)"
        self.app.store.worlds.append(w)
        self.app.store.save()
        self.refresh(len(self.app.store.worlds) - 1)

    def delete(self):
        i = self._selected()
        if i is None:
            return
        name = self.app.store.worlds[i]["name"]
        if messagebox.askyesno("NewMUD", f"Delete world '{name}' with all its triggers, aliases and macros?", parent=self):
            del self.app.store.worlds[i]
            self.app.store.save()
            self.refresh()

    def import_gmud(self):
        self.app.import_gmud(parent=self)
        self.refresh(len(self.app.store.worlds) - 1)


def monospace_fonts(root):
    fams = sorted(set(tkfont.families(root)))
    preferred = [f for f in ("Consolas", "Cascadia Mono", "Cascadia Code", "Courier New", "Lucida Console",
                             "Fixedsys", "Terminal", "DejaVu Sans Mono") if f in fams]
    others = []
    for fam in fams:
        if fam.startswith("@") or fam in preferred:
            continue
        try:
            f = tkfont.Font(root=root, family=fam, size=10)
            if f.metrics("fixed"):
                others.append(fam)
        except tk.TclError:
            pass
    return preferred + others


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.transient(app)
        self.grab_set()
        s = app.store.settings
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # Appearance
        a = ttk.Frame(nb, padding=10)
        nb.add(a, text="Appearance")
        ttk.Label(a, text="Font").grid(row=0, column=0, sticky="w", **PAD)
        self.font = ttk.Combobox(a, values=monospace_fonts(app), width=28)
        self.font.set(s["font_family"])
        self.font.grid(row=0, column=1, sticky="w", **PAD)
        ttk.Label(a, text="Size").grid(row=1, column=0, sticky="w", **PAD)
        self.size = ttk.Spinbox(a, from_=6, to=40, width=6)
        self.size.set(s["font_size"])
        self.size.grid(row=1, column=1, sticky="w", **PAD)
        self.colors = {}
        for row, (key, label) in enumerate([("background", "Background"), ("foreground", "Text"),
                                            ("echo_color", "Your commands"), ("system_color", "Client messages"),
                                            ("input_background", "Input box")], start=2):
            ttk.Label(a, text=label).grid(row=row, column=0, sticky="w", **PAD)
            cf = ColorField(a, allow_empty=False)
            cf.set(s[key])
            cf.grid(row=row, column=1, sticky="w", **PAD)
            self.colors[key] = cf
        self.bools = {}
        r = 7
        for key, label in [("bold_is_bright", "Show bold colors as bright (classic MUD look)"),
                           ("word_wrap", "Wrap long lines at word boundaries")]:
            var = tk.BooleanVar(value=s[key])
            ttk.Checkbutton(a, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w", **PAD)
            self.bools[key] = var
            r += 1

        # Behaviour
        b = ttk.Frame(nb, padding=10)
        nb.add(b, text="Input && behaviour")
        r = 0
        for key, label in [("local_echo", "Echo my commands in the output window"),
                           ("keep_input", "Keep the last command in the input box (selected)"),
                           ("numpad_walking", "Numeric keypad walks (8 north, 2 south, 4 west, 6 east, 9 up, 3 down)"),
                           ("clickable_links", "Make web links clickable"),
                           ("auto_reconnect", "Reconnect automatically when the connection drops")]:
            var = tk.BooleanVar(value=s[key])
            ttk.Checkbutton(b, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w", **PAD)
            self.bools[key] = var
            r += 1
        self.strs = {}
        for key, label in [("stack_char", "Command separator"), ("speedwalk_prefix", "Speedwalk prefix"),
                           ("command_char", "Client command prefix")]:
            ttk.Label(b, text=label).grid(row=r, column=0, sticky="w", **PAD)
            e = ttk.Entry(b, width=6)
            e.insert(0, s[key])
            e.grid(row=r, column=1, sticky="w", **PAD)
            self.strs[key] = e
            r += 1
        ttk.Label(b, text="Scrollback lines").grid(row=r, column=0, sticky="w", **PAD)
        self.scroll = ttk.Spinbox(b, from_=1000, to=500000, increment=1000, width=10)
        self.scroll.set(s["scrollback_lines"])
        self.scroll.grid(row=r, column=1, sticky="w", **PAD)
        r += 1
        ttk.Label(b, text="Log folder").grid(row=r, column=0, sticky="w", **PAD)
        lf = ttk.Frame(b)
        lf.grid(row=r, column=1, sticky="w", **PAD)
        self.logdir = tk.StringVar(value=s.get("log_folder", ""))
        ttk.Entry(lf, textvariable=self.logdir, width=30).pack(side="left")
        ttk.Button(lf, text="…", width=3,
                   command=lambda: self.logdir.set(filedialog.askdirectory(parent=self) or self.logdir.get())).pack(side="left")

        bb = ttk.Frame(self, padding=(8, 0, 8, 8))
        bb.pack(fill="x")
        ttk.Button(bb, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bb, text="OK", command=self.ok).pack(side="right", padx=4)
        center(self, app)
        self.bind("<Escape>", lambda e: self.destroy())

    def ok(self):
        s = self.app.store.settings
        s["font_family"] = self.font.get() or "Consolas"
        try:
            s["font_size"] = max(6, min(40, int(self.size.get())))
        except ValueError:
            pass
        for k, cf in self.colors.items():
            if cf.get():
                s[k] = cf.get()
        for k, var in self.bools.items():
            s[k] = bool(var.get())
        for k, e in self.strs.items():
            s[k] = e.get().strip()
        try:
            s["scrollback_lines"] = max(1000, int(self.scroll.get()))
        except ValueError:
            pass
        s["log_folder"] = self.logdir.get().strip()
        self.app.store.save()
        self.app.apply_settings()
        self.destroy()
