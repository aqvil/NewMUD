"""Aliases, triggers, speedwalking, command stacking and variables.

This module has no GUI code so it can be unit tested on its own.
"""
import re

DIRECTIONS = ("ne", "nw", "se", "sw", "n", "s", "e", "w", "u", "d")
_WALK_TOKEN = re.compile(r"(\d*)(ne|nw|se|sw|n|s|e|w|u|d)")
_VAR_REF = re.compile(r"@(\w+)")
MAX_EXPANSION = 50  # guards against aliases that call themselves forever


def split_stack(line, sep=";"):
    """Split 'a;b;c' into commands. ';;' gives a literal ';'."""
    if not sep:
        return [line]
    parts, cur, i = [], [], 0
    while i < len(line):
        if line.startswith(sep + sep, i):
            cur.append(sep)
            i += 2
        elif line.startswith(sep, i):
            parts.append("".join(cur))
            cur = []
            i += len(sep)
        else:
            cur.append(line[i])
            i += 1
    parts.append("".join(cur))
    return parts


def expand_speedwalk(text):
    """'3n2e' -> ['n','n','n','e','e'] or None if text is not a walk string."""
    text = text.replace(" ", "").lower()
    if not text:
        return None
    pos, out = 0, []
    while pos < len(text):
        m = _WALK_TOKEN.match(text, pos)
        if not m:
            return None
        count = int(m.group(1)) if m.group(1) else 1
        out.extend([m.group(2)] * min(count, 100))
        pos = m.end()
    return out


def substitute(template, args, whole=""):
    """Replace %0..%9 and %* in template."""
    def rep(m):
        key = m.group(1)
        if key == "*":
            return " ".join(args)
        idx = int(key)
        if idx == 0:
            return whole
        return args[idx - 1] if idx - 1 < len(args) else ""
    return re.sub(r"%([0-9*])", rep, template)


def wildcard_to_regex(pattern):
    out = []
    for ch in pattern:
        if ch == "*":
            out.append("(.*?)")
        elif ch == "?":
            out.append("(\\S+)")
        else:
            out.append(re.escape(ch))
    return "".join(out)


class Trigger:
    """A compiled trigger.

    kind: 'contains' (text appears anywhere), 'wildcard' (* and ? capture),
          'regex' (Python regular expression), 'exact' (whole line)
    """

    def __init__(self, data):
        self.data = data
        self.pattern = data.get("pattern", "")
        self.kind = data.get("type", "contains")
        self.send = data.get("send", "")
        self.highlight = data.get("highlight", "")
        self.gag = bool(data.get("gag", False))
        self.sound = data.get("sound", "")
        self.enabled = data.get("enabled", True)
        self.regex = None
        try:
            if self.kind == "regex":
                self.regex = re.compile(self.pattern)
            elif self.kind == "wildcard":
                self.regex = re.compile(wildcard_to_regex(self.pattern))
            elif self.kind == "exact":
                self.regex = re.compile("^" + re.escape(self.pattern) + "$")
            else:
                self.regex = re.compile(re.escape(self.pattern), re.IGNORECASE if data.get("ignore_case") else 0)
        except re.error:
            self.enabled = False

    def match(self, line):
        if not self.enabled or not self.pattern or self.regex is None:
            return None
        return self.regex.search(line)


class Automation:
    """Holds one world's aliases/triggers/variables and turns typed input into commands."""

    def __init__(self, world=None, settings=None):
        self.world = world or {}
        self.settings = settings or {}
        self.variables = self.world.setdefault("variables", {})
        self.reload()

    def reload(self):
        self.triggers = [Trigger(t) for t in self.world.get("triggers", [])]
        self.aliases = [a for a in self.world.get("aliases", []) if a.get("enabled", True) and a.get("name")]

    # ------------------------------------------------------------------ input
    @property
    def stack_char(self):
        return self.settings.get("stack_char", ";")

    @property
    def walk_prefix(self):
        return self.settings.get("speedwalk_prefix", ".")

    @property
    def cmd_char(self):
        return self.settings.get("command_char", "#")

    def process_input(self, line):
        """Return list of actions: ("send", text) or ("client", name, argstring)."""
        if line.startswith("\\"):
            return [("send", line[1:])]  # raw: no aliases / stacking
        actions = []
        self._expand(line, actions, depth=0)
        return actions

    def _expand(self, line, actions, depth):
        if depth > 10 or len(actions) > MAX_EXPANSION * 4:
            actions.append(("client", "error", "Alias loop detected - stopped expanding."))
            return
        for cmd in split_stack(line, self.stack_char):
            self._expand_one(cmd, actions, depth)

    def _expand_one(self, cmd, actions, depth):
        stripped = cmd.strip()
        cc = self.cmd_char
        # client commands
        if cc and stripped.startswith(cc) and len(stripped) > 1:
            body = stripped[len(cc):]
            m = re.match(r"(\d+)\s+(.*)", body)
            if m:  # #5 kill rat  -> repeat
                for _ in range(min(int(m.group(1)), 100)):
                    self._expand(m.group(2), actions, depth + 1)
                return
            name, _, args = body.partition(" ")
            name = name.lower()
            if name == "var":
                self._cmd_var(args, actions)
            elif name == "unvar":
                self.variables.pop(args.strip(), None)
                actions.append(("client", "echo", f"Variable '{args.strip()}' removed."))
            else:
                actions.append(("client", name, self._vars(args)))
            return
        # speedwalk
        wp = self.walk_prefix
        if wp and stripped.startswith(wp) and len(stripped) > len(wp):
            dirs = expand_speedwalk(stripped[len(wp):])
            if dirs:
                actions.extend(("send", d) for d in dirs)
                return
        # aliases
        for alias in self.aliases:
            name = alias["name"]
            repl = alias.get("replacement", "")
            if name.startswith("^"):
                try:
                    m = re.match(name, stripped)
                except re.error:
                    continue
                if m:
                    args = [g or "" for g in m.groups()]
                    self._expand(substitute(repl, args, stripped), actions, depth + 1)
                    return
                continue
            word, _, rest = stripped.partition(" ")
            if word == name:
                args = rest.split()
                if re.search(r"%[0-9*]", repl):
                    out = substitute(repl, args, stripped)
                else:
                    out = (repl + " " + rest).rstrip() if rest else repl
                self._expand(out, actions, depth + 1)
                return
        actions.append(("send", self._vars(cmd)))

    def _vars(self, text):
        if "@" not in text:
            return text
        return _VAR_REF.sub(lambda m: str(self.variables.get(m.group(1), m.group(0))), text)

    def _cmd_var(self, args, actions):
        name, _, value = args.strip().partition(" ")
        if not name:
            if not self.variables:
                actions.append(("client", "echo", "No variables set. Use #var name value"))
            for k, v in sorted(self.variables.items()):
                actions.append(("client", "echo", f"@{k} = {v}"))
        elif not value:
            actions.append(("client", "echo", f"@{name} = {self.variables.get(name, '(not set)')}"))
        else:
            self.variables[name] = self._vars(value)

    # ---------------------------------------------------------------- output
    def check_line(self, line):
        """Run triggers on one line of output (ANSI already stripped).

        Returns dict: gag (bool), highlights [(start, end, color)],
        commands [str], sounds [path]
        """
        result = {"gag": False, "highlights": [], "commands": [], "sounds": []}
        for trig in self.triggers:
            m = trig.match(line)
            if not m:
                continue
            if trig.gag:
                result["gag"] = True
            if trig.highlight:
                result["highlights"].append((m.start(), m.end(), trig.highlight))
            if trig.sound:
                result["sounds"].append(trig.sound)
            if trig.send:
                args = [g or "" for g in m.groups()]
                result["commands"].append(substitute(trig.send, args, m.group(0)))
        return result
