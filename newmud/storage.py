"""Settings and world list, saved as readable JSON.

NewMUD is portable like GMud: the data file lives next to the program
when that folder is writable, otherwise in %APPDATA%\\NewMUD.
"""
import copy
import json
import os
import sys

DATA_NAME = "newmud_data.json"

DEFAULT_SETTINGS = {
    "font_family": "Consolas",
    "font_size": 11,
    "background": "#0c0c0c",
    "foreground": "#cccccc",
    "echo_color": "#d7af5f",
    "system_color": "#5fafd7",
    "input_background": "#1a1a1a",
    "bold_is_bright": True,
    "local_echo": True,
    "keep_input": False,
    "scrollback_lines": 20000,
    "stack_char": ";",
    "speedwalk_prefix": ".",
    "command_char": "#",
    "numpad_walking": True,
    "clickable_links": True,
    "auto_reconnect": False,
    "log_folder": "",
    "word_wrap": True,
}

DEFAULT_GAUGES = [
    {"label": "HP", "regex": r"(\d+)H\b", "max_regex": "", "color": "#e74856", "enabled": True},
    {"label": "Mana", "regex": r"(\d+)M\b", "max_regex": "", "color": "#3b78ff", "enabled": True},
    {"label": "Move", "regex": r"(\d+)V\b", "max_regex": "", "color": "#16c60c", "enabled": True},
]

DEFAULT_MACROS = [
    {"key": f"F{i}", "command": "", "enabled": True} for i in range(1, 13)
]


def new_world(name="New World", host="", port=23):
    return {
        "name": name,
        "host": host,
        "port": port,
        "description": "",
        "encoding": "auto",
        "connect_commands": "",
        "aliases": [],
        "triggers": [],
        "macros": copy.deepcopy(DEFAULT_MACROS),
        "timers": [],
        "gauges": copy.deepcopy(DEFAULT_GAUGES),
        "variables": {},
        "history": [],
    }


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_path():
    portable = os.path.join(app_dir(), DATA_NAME)
    if os.path.exists(portable):
        return portable
    try:
        test = os.path.join(app_dir(), ".newmud_write_test")
        with open(test, "w") as f:
            f.write("x")
        os.remove(test)
        return portable
    except OSError:
        folder = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NewMUD")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, DATA_NAME)


class Store:
    def __init__(self, path=None):
        self.path = path or data_path()
        self.settings = dict(DEFAULT_SETTINGS)
        self.worlds = []
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.worlds = [new_world("4Dimensions", "4dimensions.org", 6000)]
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            backup = self.path + ".broken"
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.worlds = [new_world("4Dimensions", "4dimensions.org", 6000)]
            return
        self.settings.update(data.get("settings", {}))
        self.worlds = []
        for w in data.get("worlds", []):
            base = new_world()
            base.update(w)
            self.worlds.append(base)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "settings": self.settings, "worlds": self.worlds}, f, indent=2)
        os.replace(tmp, self.path)

    def find_world(self, name):
        for w in self.worlds:
            if w["name"].lower() == name.lower():
                return w
        return None
