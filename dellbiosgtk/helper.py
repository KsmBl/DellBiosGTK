#!/usr/bin/env python3
"""Privileged helper for Dell BIOS GTK.

Runs as root (started through pkexec by the GUI) and talks JSON lines over
stdin/stdout. It only ever touches files below the dell-wmi-sysman
firmware-attributes tree:

  {"op": "read"}                                   -> all attributes + state
  {"op": "set", "changes": {name: value}, "password": "..."|null}
                                                   -> per-attribute results

The helper exits when stdin is closed (i.e. when the GUI quits).
"""
import argparse
import json
import os
import re
import sys

DEFAULT_ROOT = "/sys/class/firmware-attributes/dell-wmi-sysman"
ATTR_FIELDS = (
    "type", "current_value", "default_value", "display_name",
    "possible_values", "min_value", "max_value", "scalar_increment",
    "min_length", "max_length", "dell_modifier", "dell_value_modifier",
)
AUTH_FIELDS = ("is_enabled", "role", "mechanism",
               "min_password_length", "max_password_length")
MOD_RE = re.compile(r"\[\w+:([^=\]]+)=[^\]]*\]")


def read_file(path):
    try:
        with open(path) as f:
            return f.read().rstrip("\n")
    except OSError:
        return None


def write_file(path, value):
    # sysfs expects the whole value in a single write() call
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


class Bios:
    def __init__(self, root):
        self.root = root
        self.attr_dir = os.path.join(root, "attributes")
        self.auth_dir = os.path.join(root, "authentication")

    def names(self):
        return sorted(n for n in os.listdir(self.attr_dir)
                      if os.path.isdir(os.path.join(self.attr_dir, n)))

    def read(self):
        attrs = {}
        for name in self.names():
            d = os.path.join(self.attr_dir, name)
            info = {}
            for field in ATTR_FIELDS:
                v = read_file(os.path.join(d, field))
                if v is not None:
                    info[field] = v
            attrs[name] = info
        auth = {}
        if os.path.isdir(self.auth_dir):
            for role in sorted(os.listdir(self.auth_dir)):
                d = os.path.join(self.auth_dir, role)
                auth[role] = {f: read_file(os.path.join(d, f)) for f in AUTH_FIELDS}
        return {
            "ok": True,
            "attrs": attrs,
            "auth": auth,
            "pending_reboot": read_file(os.path.join(self.attr_dir, "pending_reboot")),
            "is_root": os.geteuid() == 0,
        }

    def set(self, changes, password=None):
        if not isinstance(changes, dict) or not changes:
            raise ValueError("no changes given")
        valid = set(self.names())
        for name, value in changes.items():
            if name not in valid:
                raise ValueError(f"unknown attribute: {name!r}")
            if not isinstance(value, str) or "\n" in value or len(value) > 512:
                raise ValueError(f"invalid value for {name}")

        # Attributes other settings depend on (e.g. PrimaryBattChargeCfg must
        # become "Custom" before CustomChargeStart is writable) go first.
        deps = set()
        for name in valid:
            mod = read_file(os.path.join(self.attr_dir, name, "dell_modifier")) or ""
            deps.update(MOD_RE.findall(mod))
        order = sorted(changes, key=lambda n: (n not in deps, n))

        pw_path = os.path.join(self.auth_dir, "Admin", "current_password")
        results = {}
        if password:
            write_file(pw_path, password)
        try:
            for name in order:
                path = os.path.join(self.attr_dir, name, "current_value")
                try:
                    write_file(path, changes[name])
                    results[name] = {"ok": True}
                except OSError as e:
                    results[name] = {"ok": False, "error": e.strerror or str(e)}
        finally:
            if password:
                try:
                    write_file(pw_path, "\n")  # clear the cached password
                except OSError:
                    pass
        return {"ok": True, "results": results, "order": order}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="firmware-attributes device dir (for testing)")
    args = ap.parse_args()
    bios = Bios(args.root)
    if not os.path.isdir(bios.attr_dir):
        print(json.dumps({"ok": False, "error": f"{bios.attr_dir} not found "
                          "(is the dell-wmi-sysman module loaded?)"}), flush=True)
        return 1

    print(json.dumps({"ok": True, "ready": True}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "read":
                resp = bios.read()
            elif op == "set":
                resp = bios.set(req.get("changes"), req.get("password"))
            else:
                resp = {"ok": False, "error": f"unknown op {op!r}"}
        except Exception as e:  # report, never crash the session
            resp = {"ok": False, "error": str(e)}
        print(json.dumps(resp), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
