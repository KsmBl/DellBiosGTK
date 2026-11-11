#!/usr/bin/env python3
"""Dell BIOS GTK — edit Dell BIOS settings from a running Linux system.

A classic GTK 3 desktop application in the style of Thunar / failBrauwser:
menu bar, toolbar, section tree on the left, settings on the right and a
status bar. Native widgets only, so it follows the desktop's GTK theme,
icon theme and window-manager decorations.

Settings come from the kernel's dell-wmi-sysman firmware-attributes
interface. All root access goes through helper.py, started once per
session via pkexec.
"""
import json
import os
import re
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "local.DellBiosGTK"
APP_TITLE = "Dell BIOS Settings"
VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "helper.py")
SYSFS_ROOT = "/sys/class/firmware-attributes/dell-wmi-sysman"
# Set DELLBIOS_ROOT to a fake sysfs tree to test without root / pkexec.
TEST_ROOT = os.environ.get("DELLBIOS_ROOT")

MOD_RE = re.compile(r"\[(\w+):([^=\]]+)=([^\]]*)\]")
READ_ONLY = {"SvcTag"}

# Sidebar tree: (section, icons, [(subsection, icons, keywords)]).
# An attribute goes to the first subsection (in this order) with a keyword
# that is a case-insensitive substring of its name.
SECTIONS = [
    ("Power", ["battery", "gnome-power-manager", "xfce4-power-manager-settings"], [
        ("Battery", ["battery", "battery-good"],
         ["PrimaryBattChargeCfg", "AdvBatteryChargeCfg", "CustomCharge", "PeakShift"]),
        ("Power-On & Wake", ["system-shutdown", "system-log-out"],
         ["AutoOn", "Wake", "PowerOnLidOpen", "LidSwitch", "BlockSleep",
          "UsbPowerShare", "PowerWarn"]),
    ]),
    ("Boot", ["system-reboot", "system-restart"], [
        ("Boot Behaviour", ["system-reboot", "system-run"],
         ["Fastboot", "ExtPostTime", "Logo", "SignOfLife", "WarningsAndErr",
          "DockWarnings", "AdvancedMode", "UnobtrusiveMode", "UsbEmu", "SmartErrors",
          "NumLock", "ThunderboltBoot", "SdCardBoot"]),
        ("Network Boot", ["network-server", "network-wired"],
         ["HttpsBoot", "Pxe", "IPv", "UefiNwStack"]),
        ("Recovery", ["system-software-update", "edit-undo"],
         ["Recovery", "Rcvr"]),
    ]),
    ("Security", ["security-high", "system-lock-screen", "dialog-password"], [
        ("Secure Boot", ["security-high", "channel-secure"],
         ["SecureBoot", "MSUefiCA", "UefiBootPath"]),
        ("TPM", ["security-medium", "application-certificate"],
         ["Tpm", "SHA256"]),
        ("Passwords", ["dialog-password", "system-lock-screen"],
         ["Pwd", "Password", "AdminSetupLockout", "Psid"]),
        ("Chassis Intrusion", ["dialog-warning"],
         ["Chas", "Intrusion"]),
        ("DMA Protection", ["security-medium", "security-high"],
         ["Dma"]),
        ("Firmware & Anti-Theft", ["system-software-update", "software-update-available"],
         ["Capsule", "Downgrade", "FOTA", "BIOSConnect", "Tamper", "Smm", "TME",
          "LegacyInterface", "Absolute", "ABI", "Wipe"]),
    ]),
    ("Performance", ["utilities-system-monitor", "applications-system"], [
        ("Processor", ["cpu", "utilities-system-monitor", "applications-system"],
         ["Cpu", "Atom", "LogicProc", "Turbo", "Speed", "CStates", "Sagv", "Itbm"]),
        ("Thermal", ["temperature", "weather-clear", "utilities-system-monitor"],
         ["ThermalManagement"]),
        ("Graphics & PCIe", ["video-display", "preferences-desktop-display"],
         ["Graphic", "Mmio", "Resizable", "Aspm", "VideoPowerOnly"]),
        ("Virtualization", ["computer", "virt-manager"],
         ["Virtualization", "VtFor", "TrustExecution"]),
    ]),
    ("Devices", ["computer", "drive-harddisk"], [
        ("Storage", ["drive-harddisk", "drive-harddisk-solidstate"],
         ["Sata", "M2Pcie", "SdCard"]),
        ("USB & Thunderbolt", ["drive-removable-media", "drive-removable-media-usb"],
         ["Usb", "Thunderbolt", "TypeCDockOverride", "DisUsb4"]),
        ("Audio & Camera", ["audio-card", "camera-web", "camera-photo"],
         ["Camera", "Microphone", "Audio", "Speaker", "MicMute"]),
        ("Keyboard", ["input-keyboard", "preferences-desktop-keyboard"],
         ["FnLock", "Kbd", "Keyboard", "Hotkey"]),
        ("Wireless", ["network-wireless", "bluetooth"],
         ["Wireless", "Wlan", "Wwan", "Bluetooth", "Nfc"]),
        ("Wired Network", ["network-wired", "network-transmit-receive"],
         ["Nic", "MacAddr", "TypeCDockLan"]),
    ]),
    ("Maintenance", ["preferences-system", "applications-utilities"], [
        ("Service & Asset Tag", ["dialog-information", "help-about"],
         ["Asset", "SvcTag", "SupportAssistSupport", "Telemetry"]),
        ("Logs", ["text-x-generic", "accessories-text-editor"],
         ["LogClear"]),
    ]),
]
OTHER = ("Other", ["applications-other", "preferences-other"])
ALL_KEY = "all"
CHANGED_KEY = "changed"


def categorize(name):
    """Return (section, subsection) for an attribute name."""
    low = name.lower()
    for section, _icons, subs in SECTIONS:
        for sub, _sicons, keys in subs:
            if any(k.lower() in low for k in keys):
                return section, sub
    return OTHER[0], OTHER[0]


def pick_icon(candidates, fallback="preferences-system"):
    theme = Gtk.IconTheme.get_default()
    for name in candidates:
        if theme.has_icon(name):
            return name
    return fallback


def read_dmi(field):
    try:
        with open(f"/sys/class/dmi/id/{field}") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class HelperError(Exception):
    pass


class HelperClient:
    """Long-running root helper; one pkexec prompt per session."""

    def __init__(self):
        if TEST_ROOT:
            cmd = [sys.executable, "-I", HELPER, "--root", TEST_ROOT]
        else:
            cmd = ["pkexec", sys.executable, "-I", HELPER]
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        hello = self.proc.stdout.readline()
        if not hello:
            code = self.proc.wait()
            err = self.proc.stderr.read().strip()
            if code == 126:
                raise HelperError("Authentication was cancelled.")
            if code == 127:
                raise HelperError("Not authorized (or no polkit agent is running).\n" + err)
            raise HelperError(err or f"Helper exited with code {code}.")
        msg = json.loads(hello)
        if not msg.get("ok"):
            raise HelperError(msg.get("error", "Helper failed to start."))

    def call(self, **req):
        with self.lock:
            if self.proc.poll() is not None:
                raise HelperError("The privileged helper is no longer running.")
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        if not line:
            raise HelperError("The privileged helper exited unexpectedly.")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise HelperError(resp.get("error", "Unknown helper error."))
        return resp

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


class Setting:
    def __init__(self, name, info):
        self.name = name
        self.info = info
        self.type = info.get("type", "")
        self.display = info.get("display_name") or name
        self.current = info.get("current_value")
        self.default = info.get("default_value", "")
        self.pending = None
        self.mods = MOD_RE.findall(info.get("dell_modifier", ""))
        self.fixed_readonly = name in READ_ONLY or self.current is None
        self.section, self.sub = categorize(name)
        self.options = []
        self.row = self.control = self.title = self.subtitle = self.revert_btn = None
        self.base_subtitle = ""

    @property
    def effective(self):
        return self.current if self.pending is None else self.pending


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(1000, 700)
        self.set_icon_name(pick_icon(["preferences-system", "computer"]))
        self.helper = None
        self.data = None
        self.settings = {}
        self.sub_headers = {}   # (section, sub) -> header ListBoxRow
        self.nav_iters = {}     # key -> TreeIter
        self.current_nav = ALL_KEY
        self._guard = False
        self._busy = False

        self.add_actions()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(Gtk.MenuBar.new_from_model(self.menubar_model()), False, False, 0)
        vbox.pack_start(self.build_toolbar(), False, False, 0)

        self.reboot_bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO, no_show_all=True)
        self.reboot_bar.get_content_area().add(
            Gtk.Label(label="BIOS changes are pending. They take effect after the next restart."))
        self.reboot_bar.add_button("_Restart Now", 1)
        self.reboot_bar.connect("response", lambda *_: self.on_reboot())
        vbox.pack_start(self.reboot_bar, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=250)
        paned.pack1(self.build_sidebar(), False, False)
        paned.pack2(self.build_content(), True, False)
        vbox.pack_start(paned, True, True, 0)

        self.statusbar = Gtk.Statusbar()
        self.status_ctx = self.statusbar.get_context_id("main")
        vbox.pack_start(self.statusbar, False, False, 0)

        self.add(vbox)
        self.connect("delete-event", self.on_delete)
        self.show_all()
        self.update_state()
        if TEST_ROOT or os.path.isdir(SYSFS_ROOT):
            GLib.idle_add(self.on_unlock)
        else:
            self.show_message_page(
                "dialog-warning", "No Dell BIOS interface found",
                f"{SYSFS_ROOT} does not exist.\nThis needs a Dell computer with the "
                "dell-wmi-sysman kernel module loaded (sudo modprobe dell-wmi-sysman).")

    # ---- actions / menus -------------------------------------------------
    def add_actions(self):
        def act(name, cb, accels=()):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)
            if accels:
                self.get_application().set_accels_for_action(f"win.{name}", list(accels))
            return a

        def toggle(name, accels=()):
            a = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(False))
            a.connect("change-state", self.on_toggle)
            self.add_action(a)
            if accels:
                self.get_application().set_accels_for_action(f"win.{name}", list(accels))
            return a

        self.act_apply = act("apply", self.on_apply, ["<Control>s"])
        self.act_discard = act("discard", self.discard_all)
        self.act_reload = act("reload", self.reload, ["F5", "<Control>r"])
        self.act_unlock = act("unlock", self.on_unlock)
        act("close", self.close, ["<Control>w", "<Control>q"])
        self.act_defaults = act("defaults", self.stage_defaults)
        act("find", lambda: self.search_entry.grab_focus(), ["<Control>f"])
        act("expand", lambda: self.tree.expand_all())
        act("collapse", lambda: self.tree.collapse_all())
        act("about", self.on_about, ["F1"])
        self.act_show_hidden = toggle("show-hidden", ["<Control>h"])
        self.act_show_names = toggle("show-names")
        self.act_show_names.set_state(GLib.Variant.new_boolean(True))

    def menubar_model(self):
        def section(*items):
            m = Gio.Menu()
            for label, action in items:
                m.append(label, action)
            return m

        bar = Gio.Menu()
        file = Gio.Menu()
        file.append_section(None, section(("_Unlock…", "win.unlock")))
        file.append_section(None, section(("_Apply Changes", "win.apply"),
                                          ("_Discard Changes", "win.discard"),
                                          ("_Reload from BIOS", "win.reload")))
        file.append_section(None, section(("_Close", "win.close")))
        bar.append_submenu("_File", file)
        edit = Gio.Menu()
        edit.append_section(None, section(("_Find Setting", "win.find")))
        edit.append_section(None, section(("Reset Shown Settings to _Defaults", "win.defaults")))
        bar.append_submenu("_Edit", edit)
        view = Gio.Menu()
        view.append_section(None, section(("Show _Hidden Settings", "win.show-hidden"),
                                          ("Show _Attribute Names", "win.show-names")))
        view.append_section(None, section(("_Expand All Sections", "win.expand"),
                                          ("C_ollapse All Sections", "win.collapse")))
        bar.append_submenu("_View", view)
        bar.append_submenu("_Help", section(("_About", "win.about")))
        return bar

    def build_toolbar(self):
        tb = Gtk.Toolbar(toolbar_style=Gtk.ToolbarStyle.BOTH_HORIZ)
        tb.get_style_context().add_class(Gtk.STYLE_CLASS_PRIMARY_TOOLBAR)

        def btn(label, icons, action, tip, important=True):
            b = Gtk.ToolButton(label=label, icon_name=pick_icon(icons))
            b.set_action_name(action)
            b.set_tooltip_text(tip)
            b.set_is_important(important)
            tb.insert(b, -1)
            return b

        btn("Apply", ["document-save", "dialog-apply", "gtk-apply"], "win.apply",
            "Write the changed settings to the BIOS (Ctrl+S)")
        btn("Discard", ["edit-clear", "edit-undo", "gtk-revert-to-saved"], "win.discard",
            "Throw away all unsaved changes")
        tb.insert(Gtk.SeparatorToolItem(), -1)
        btn("Reload", ["view-refresh", "gtk-refresh"], "win.reload",
            "Read all settings from the BIOS again (F5)", important=False)
        spacer = Gtk.SeparatorToolItem(draw=False)
        spacer.set_expand(True)
        tb.insert(spacer, -1)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Find a setting…", width_chars=28)
        self.search_entry.connect("search-changed", lambda *_: self.refresh())
        item = Gtk.ToolItem()
        item.add(self.search_entry)
        tb.insert(item, -1)
        return tb

    def build_sidebar(self):
        # key, label, icon, weight, badge
        self.store = Gtk.TreeStore(str, str, str, int, str)
        self.tree = Gtk.TreeView(model=self.store, headers_visible=False, enable_search=False)
        col = Gtk.TreeViewColumn()
        icon = Gtk.CellRendererPixbuf()
        text = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        badge = Gtk.CellRendererText(xalign=1.0, weight=Pango.Weight.BOLD)
        col.pack_start(icon, False)
        col.pack_start(text, True)
        col.pack_end(badge, False)
        col.add_attribute(icon, "icon-name", 2)
        col.add_attribute(text, "text", 1)
        col.add_attribute(text, "weight", 3)
        col.add_attribute(badge, "text", 4)
        self.tree.append_column(col)
        self.tree.get_selection().connect("changed", self.on_nav_changed)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, shadow_type=Gtk.ShadowType.IN)
        sw.add(self.tree)
        sw.set_size_request(200, -1)
        return sw

    def build_content(self):
        self.content_stack = Gtk.Stack()
        # message page (locked / errors / loading)
        msg = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.msg_icon = Gtk.Image(pixel_size=64)
        self.msg_title = Gtk.Label()
        self.msg_text = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60)
        self.msg_spinner = Gtk.Spinner(no_show_all=True)
        self.msg_button = Gtk.Button(label="Unlock…", halign=Gtk.Align.CENTER, no_show_all=True)
        self.msg_button.set_action_name("win.unlock")
        for w in (self.msg_icon, self.msg_title, self.msg_text, self.msg_spinner, self.msg_button):
            msg.pack_start(w, False, False, 0)
        self.content_stack.add_named(msg, "message")

        # settings page
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.location_label = Gtk.Label(xalign=0, margin_start=12, margin_end=12,
                                        margin_top=8, margin_bottom=8,
                                        ellipsize=Pango.EllipsizeMode.END)
        page.pack_start(self.location_label, False, False, 0)
        page.pack_start(Gtk.Separator(), False, False, 0)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_header_func(self._row_separator)
        self.empty_label = Gtk.Label(margin=24, no_show_all=True)
        self.empty_label.get_style_context().add_class("dim-label")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.pack_start(self.listbox, False, False, 0)
        inner.pack_start(self.empty_label, False, False, 0)
        self.list_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list_scroller.add(inner)
        page.pack_start(self.list_scroller, True, True, 0)
        self.content_stack.add_named(page, "settings")

        frame = Gtk.Frame(shadow_type=Gtk.ShadowType.IN)
        frame.add(self.content_stack)
        return frame

    @staticmethod
    def _row_separator(row, before):
        if before is not None and row.get_header() is None:
            row.set_header(Gtk.Separator())

    def show_message_page(self, icon, title, text, button=False, spinner=False):
        self.msg_icon.set_from_icon_name(pick_icon([icon, "dialog-information"]), Gtk.IconSize.DIALOG)
        self.msg_icon.set_pixel_size(64)
        self.msg_title.set_markup(f"<big><b>{GLib.markup_escape_text(title)}</b></big>")
        self.msg_text.set_text(text)
        self.msg_button.set_visible(button)
        self.msg_spinner.set_visible(spinner)
        if spinner:
            self.msg_spinner.start()
        else:
            self.msg_spinner.stop()
        self.content_stack.set_visible_child_name("message")

    # ---- background work -------------------------------------------------
    def run_bg(self, fn, on_ok, on_err=None):
        def worker():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(on_err or self.on_error, e)
            else:
                GLib.idle_add(on_ok, result)
        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, busy, text=None):
        self._busy = busy
        win = self.get_window()
        if win:
            from gi.repository import Gdk
            win.set_cursor(Gdk.Cursor.new_from_name(win.get_display(), "wait") if busy else None)
        if text:
            self.set_status(text)
        self.update_state()

    def set_status(self, text):
        self.statusbar.remove_all(self.status_ctx)
        self.statusbar.push(self.status_ctx, text)

    def message(self, kind, title, text):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                buttons=Gtk.ButtonsType.OK, text=title, secondary_text=text)
        dlg.run()
        dlg.destroy()

    def on_error(self, err):
        self.set_busy(False)
        if self.helper and self.helper.proc.poll() is not None:
            self.helper = None
        if self.helper is None or self.data is None:
            self.data = None
            self.show_message_page("system-lock-screen", "Could not access the BIOS", str(err),
                                   button=True)
        else:
            self.message(Gtk.MessageType.ERROR, "Error", str(err))
        self.update_state()

    # ---- loading ---------------------------------------------------------
    def on_unlock(self):
        if self.helper and self.data is not None:
            return
        self.show_message_page("system-lock-screen", "Waiting for authentication…",
                               "Reading and changing firmware settings requires "
                               "administrator rights.", spinner=True)
        self.set_busy(True, "Waiting for authentication…")

        def work():
            if self.helper is None:
                self.helper = HelperClient()
            GLib.idle_add(self.set_status, "Reading BIOS settings…")
            return self.helper.call(op="read")
        self.run_bg(work, self.on_loaded)

    def reload(self):
        if not self.helper or self._busy:
            return
        self.set_busy(True, "Reading BIOS settings…")
        self.run_bg(lambda: self.helper.call(op="read"), self.on_loaded)

    def on_loaded(self, data):
        self.set_busy(False)
        self.data = data
        self.build(data)
        self.content_stack.set_visible_child_name("settings")
        self.reboot_bar.set_visible(data.get("pending_reboot") == "1")
        if not data.get("is_root") and not TEST_ROOT:
            self.message(Gtk.MessageType.WARNING, "Not running as root",
                         "The helper is not running as root; values may be missing.")

    # ---- building --------------------------------------------------------
    def build(self, data):
        self.settings = {n: Setting(n, i) for n, i in data["attrs"].items()}
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.sub_headers = {}

        order = [(sec, sub) for sec, _i, subs in SECTIONS for sub, _si, _k in subs]
        order.append((OTHER[0], OTHER[0]))
        by_sub = {}
        for s in self.settings.values():
            by_sub.setdefault((s.section, s.sub), []).append(s)
        for key in order:
            members = sorted(by_sub.get(key, []), key=lambda s: s.display.lower())
            if not members:
                continue
            header = Gtk.ListBoxRow(activatable=False, selectable=False)
            lbl = Gtk.Label(xalign=0, margin_start=12, margin_top=14, margin_bottom=4)
            title = key[1] if key[0] == key[1] else f"{key[0]} › {key[1]}"
            lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
            header.add(lbl)
            header.key = key
            self.listbox.add(header)
            self.sub_headers[key] = header
            for s in members:
                self.listbox.add(self.make_row(s))
        self.listbox.show_all()
        self.build_nav(by_sub)
        self.refresh()

    def build_nav(self, by_sub):
        keep = self.current_nav
        sel = self.tree.get_selection()
        self._guard = True
        self.store.clear()
        self.nav_iters = {}
        self.nav_iters[ALL_KEY] = self.store.append(
            None, [ALL_KEY, "All Settings", pick_icon(["preferences-system", "computer"]),
                   Pango.Weight.NORMAL, ""])
        for sec, icons, subs in SECTIONS + [(OTHER[0], OTHER[1], [(OTHER[0], OTHER[1], [])])]:
            present = [(sub, si) for sub, si, _k in subs if by_sub.get((sec, sub))]
            if not present:
                continue
            if sec == OTHER[0]:
                self.nav_iters[f"{sec}/{sec}"] = self.store.append(
                    None, [f"{sec}/{sec}", sec, pick_icon(icons), Pango.Weight.NORMAL, ""])
                continue
            parent = self.store.append(None, [sec, sec, pick_icon(icons), Pango.Weight.BOLD, ""])
            self.nav_iters[sec] = parent
            for sub, si in present:
                key = f"{sec}/{sub}"
                self.nav_iters[key] = self.store.append(
                    parent, [key, sub, pick_icon(si), Pango.Weight.NORMAL, ""])
        self.nav_iters[CHANGED_KEY] = self.store.append(
            None, [CHANGED_KEY, "Unsaved Changes", pick_icon(["document-edit", "accessories-text-editor"]),
                   Pango.Weight.NORMAL, ""])
        self.tree.expand_all()
        self._guard = False
        self.current_nav = keep if keep in self.nav_iters else ALL_KEY
        sel.select_iter(self.nav_iters[self.current_nav])

    def make_row(self, s):
        info = s.info
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.setting = s
        hb = Gtk.Box(spacing=12, margin_start=24, margin_end=12, margin_top=6, margin_bottom=6)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        s.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        s.subtitle = Gtk.Label(xalign=0, wrap=True)
        s.subtitle.get_style_context().add_class("dim-label")
        texts.pack_start(s.title, False, False, 0)
        texts.pack_start(s.subtitle, False, False, 0)
        hb.pack_start(texts, True, True, 0)

        if s.type == "enumeration":
            s.options = [v for v in info.get("possible_values", "").split(";") if v]
            if s.current is not None and s.current not in s.options:
                s.options.append(s.current)
            ctl = Gtk.ComboBoxText(valign=Gtk.Align.CENTER)
            for o in s.options:
                ctl.append(o, o)
            if s.current in s.options:
                ctl.set_active_id(s.current)
            ctl.connect("changed", lambda c: self.set_pending(s, c.get_active_id()))
            ctl.set_size_request(190, -1)
            base = "Default: " + s.default if s.default else ""
        elif s.type == "integer":
            lo = int(info.get("min_value") or 0)
            hi = int(info.get("max_value") or 2**31 - 1)
            step = int(info.get("scalar_increment") or 1) or 1
            ctl = Gtk.SpinButton.new_with_range(lo, hi, step)
            ctl.set_valign(Gtk.Align.CENTER)
            ctl.set_numeric(True)
            if s.current and s.current.lstrip("-").isdigit():
                ctl.set_value(int(s.current))
            ctl.connect("value-changed", lambda c: self.set_pending(s, str(c.get_value_as_int())))
            base = f"Range {lo}–{hi}" + (f", default: {s.default}" if s.default else "")
        else:
            ctl = Gtk.Entry(text=s.current or "", valign=Gtk.Align.CENTER, width_chars=24)
            if info.get("max_length", "").isdigit():
                ctl.set_max_length(int(info["max_length"]))
            ctl.connect("changed", lambda c: self.set_pending(s, c.get_text()))
            base = f"{info.get('min_length', '?')}–{info.get('max_length', '?')} characters"
        s.base_subtitle = base
        if info.get("dell_value_modifier"):
            ctl.set_tooltip_text("Value rules: " + info["dell_value_modifier"])

        revert = Gtk.Button(relief=Gtk.ReliefStyle.NONE, valign=Gtk.Align.CENTER,
                            tooltip_text="Undo this change", no_show_all=True)
        revert.set_image(Gtk.Image.new_from_icon_name(pick_icon(["edit-undo", "gtk-undo"]),
                                                      Gtk.IconSize.BUTTON))
        revert.connect("clicked", lambda *_: self.revert(s))
        hb.pack_end(ctl, False, False, 0)
        hb.pack_end(revert, False, False, 0)
        row.add(hb)
        s.row, s.control, s.revert_btn = row, ctl, revert
        return row

    # ---- navigation ------------------------------------------------------
    def on_nav_changed(self, selection):
        if self._guard:
            return
        model, it = selection.get_selected()
        if it is None:
            return
        self.current_nav = model[it][0]
        if self.search_entry.get_text():
            self._guard = True
            self.search_entry.set_text("")
            self._guard = False
        self.refresh()
        self.list_scroller.get_vadjustment().set_value(0)

    def on_toggle(self, action, value):
        action.set_state(value)
        self.refresh()

    def nav_matches(self, s):
        nav = self.current_nav
        if nav == ALL_KEY:
            return True
        if nav == CHANGED_KEY:
            return s.pending is not None
        if "/" in nav:
            return nav == f"{s.section}/{s.sub}"
        return s.section == nav

    def nav_title(self):
        nav = self.current_nav
        if nav == ALL_KEY:
            return "All Settings"
        if nav == CHANGED_KEY:
            return "Unsaved Changes"
        sec, _, sub = nav.partition("/")
        return sec if not sub or sub == sec else f"{sec} › {sub}"

    # ---- value changes ---------------------------------------------------
    def set_pending(self, s, value):
        if self._guard or value is None:
            return
        s.pending = None if value == s.current else value
        self.refresh()

    def set_widget(self, s, value):
        self._guard = True
        try:
            if s.type == "enumeration":
                s.control.set_active_id(value)
            elif s.type == "integer" and value:
                s.control.set_value(int(value))
            else:
                s.control.set_text(value or "")
        finally:
            self._guard = False

    def revert(self, s):
        s.pending = None
        self.set_widget(s, s.current)
        self.refresh()

    def discard_all(self):
        for s in self.settings.values():
            if s.pending is not None:
                s.pending = None
                self.set_widget(s, s.current)
        self.refresh()

    def stage_defaults(self):
        """Stage the BIOS default value for every visible, editable setting."""
        staged = 0
        for s in self.settings.values():
            if not (s.row.get_visible() and s.control.get_sensitive() and s.default):
                continue
            if s.type == "enumeration" and s.default not in s.options:
                continue
            if s.effective != s.default:
                self.set_widget(s, s.default)
                s.pending = None if s.default == s.current else s.default
                staged += 1
        self.refresh()
        self.set_status(f"{staged} setting{'s' if staged != 1 else ''} set to default "
                        "(not yet written — click Apply)")

    # ---- dependency rules / filtering ------------------------------------
    def eval_mods(self, s):
        """Return (suppressed, lock_reasons) from the BIOS dell_modifier rules."""
        suppressed, reasons = False, []
        for kind, attr, val in s.mods:
            other = self.settings.get(attr)
            if other is None:
                continue
            eq = other.effective == val
            if kind == "SuppressIf" and eq or kind == "SuppressIfNot" and not eq:
                suppressed = True
            elif kind == "ReadOnlyIf" and eq:
                reasons.append(f"unavailable while “{other.display}” is {val}")
            elif kind == "ReadOnlyIfNot" and not eq:
                reasons.append(f"requires “{other.display}” = {val}")
        return suppressed, reasons

    def refresh(self):
        if not self.settings or self._guard:
            self.update_state()
            return
        query = self.search_entry.get_text().strip().lower()
        show_hidden = self.act_show_hidden.get_state().get_boolean()
        show_names = self.act_show_names.get_state().get_boolean()
        counts = {}
        visible_subs = set()
        total_visible = 0
        for s in self.settings.values():
            suppressed, reasons = self.eval_mods(s)
            if s.fixed_readonly:
                reasons.insert(0, "read-only" if s.current is not None else "value not readable")
            modified = s.pending is not None
            if modified:
                for k in (s.section, f"{s.section}/{s.sub}", ALL_KEY, CHANGED_KEY):
                    counts[k] = counts.get(k, 0) + 1

            title = GLib.markup_escape_text(s.display)
            s.title.set_markup(f"<b>{title}</b>" if modified else title)
            parts = [p for p in (s.name if show_names else "", s.base_subtitle) if p]
            if suppressed:
                parts.append("hidden by the BIOS")
            line = "  ·  ".join(parts)
            if reasons:
                line += ("\n" if line else "") + "Locked: " + "; ".join(reasons)
            s.subtitle.set_markup(f"<small>{GLib.markup_escape_text(line)}</small>")
            s.subtitle.set_visible(bool(line))
            s.control.set_sensitive(not reasons)
            s.revert_btn.set_visible(modified)

            if query:
                visible = query in s.display.lower() or query in s.name.lower()
            else:
                visible = self.nav_matches(s) and (not suppressed or show_hidden or modified)
            s.row.set_visible(visible)
            if visible:
                visible_subs.add((s.section, s.sub))
                total_visible += 1

        single_sub = len(visible_subs) == 1 and "/" in self.current_nav and not query
        for key, header in self.sub_headers.items():
            header.set_visible(key in visible_subs and not single_sub)

        for key, it in self.nav_iters.items():
            n = counts.get(key, 0)
            self.store[it][4] = f"({n})" if n else ""

        where = f"Search results for “{self.search_entry.get_text().strip()}”" if query else self.nav_title()
        self.location_label.set_markup(f"<big><b>{GLib.markup_escape_text(where)}</b></big>  "
                                       f"<span alpha='60%'>{total_visible} "
                                       f"setting{'s' if total_visible != 1 else ''}</span>")
        self.empty_label.set_text("No unsaved changes." if self.current_nav == CHANGED_KEY and not query
                                  else "No settings found.")
        self.empty_label.set_visible(total_visible == 0)
        self.update_state()

    def pending_changes(self):
        return {n: s.pending for n, s in self.settings.items() if s.pending is not None}

    def update_state(self):
        loaded = self.data is not None and not self._busy
        n = len(self.pending_changes()) if self.settings else 0
        self.act_apply.set_enabled(loaded and n > 0)
        self.act_discard.set_enabled(loaded and n > 0)
        self.act_reload.set_enabled(loaded)
        self.act_defaults.set_enabled(loaded)
        self.act_unlock.set_enabled(self.data is None and not self._busy)
        if self._busy:
            return
        if self.data is None:
            self.set_status("Not connected to the BIOS")
            return
        model = " ".join(p for p in (read_dmi("sys_vendor"), read_dmi("product_name")) if p)
        bios = read_dmi("bios_version")
        info = f"{model} · BIOS {bios}" if bios else model
        changes = f"{n} unsaved change{'s' if n != 1 else ''}" if n else "No unsaved changes"
        self.set_status(f"{len(self.settings)} settings · {changes}" + (f" · {info}" if info else ""))

    # ---- applying --------------------------------------------------------
    def on_apply(self):
        changes = self.pending_changes()
        if not changes:
            return
        for name, value in changes.items():
            s = self.settings[name]
            if s.type == "string":
                lo = int(s.info.get("min_length") or 0)
                hi = int(s.info.get("max_length") or 512)
                if not lo <= len(value) <= hi:
                    self.message(Gtk.MessageType.ERROR, "Invalid value",
                                 f"“{s.display}” must be {lo}–{hi} characters long.")
                    return
        lines = [f"•  {self.settings[n].display}:  {self.settings[n].current}  →  {v}"
                 for n, v in sorted(changes.items(), key=lambda kv: self.settings[kv[0]].display)]
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Write {len(changes)} setting{'s' if len(changes) != 1 else ''} to the BIOS?",
            secondary_text="\n".join(lines) + "\n\nMost changes take effect after the next restart. "
                           "Wrong firmware settings can stop the computer from starting.")
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Apply", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        pw_entry = None
        admin = (self.data or {}).get("auth", {}).get("Admin") or {}
        if admin.get("is_enabled") == "1":
            box = Gtk.Box(spacing=8)
            box.pack_start(Gtk.Label(label="BIOS admin password:"), False, False, 0)
            pw_entry = Gtk.Entry(visibility=False, activates_default=False)
            box.pack_start(pw_entry, True, True, 0)
            dlg.get_message_area().pack_start(box, False, False, 0)
            box.show_all()
        response = dlg.run()
        password = pw_entry.get_text() if pw_entry else None
        dlg.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.set_busy(True, "Writing settings to the BIOS…")
        self.run_bg(lambda: self.helper.call(op="set", changes=changes, password=password),
                    self.on_applied)

    def on_applied(self, resp):
        self.set_busy(False)
        results = resp.get("results", {})
        failed = {n: r.get("error", "?") for n, r in results.items() if not r.get("ok")}
        ok = len(results) - len(failed)
        if failed:
            body = "\n".join(f"•  {self.settings[n].display} ({n}): {e}" for n, e in failed.items())
            self.message(Gtk.MessageType.WARNING,
                         f"{len(failed)} setting{'s' if len(failed) != 1 else ''} rejected by the BIOS",
                         body + "\n\nThe BIOS may refuse a value because of another setting, "
                                "a BIOS password, or because the setting is locked.")
        self.reload()
        if ok:
            GLib.timeout_add(600, lambda: self.set_status(
                f"{ok} setting{'s' if ok != 1 else ''} written to the BIOS") and False)

    # ---- misc ------------------------------------------------------------
    def on_reboot(self):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text="Restart the computer now?",
                                secondary_text="Unsaved work in other applications will be lost.")
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Restart", Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            subprocess.Popen(["systemctl", "reboot"])
        dlg.destroy()

    def on_about(self):
        dlg = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_TITLE,
                              version=VERSION, logo_icon_name=pick_icon(["preferences-system"]),
                              comments="Change Dell BIOS settings from a running Linux system "
                                       "through the dell-wmi-sysman firmware-attributes interface.")
        dlg.run()
        dlg.destroy()

    def on_delete(self, *_):
        if self.settings and self.pending_changes():
            dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                    message_type=Gtk.MessageType.WARNING,
                                    buttons=Gtk.ButtonsType.NONE,
                                    text="Close without applying your changes?",
                                    secondary_text="The changed settings have not been written to the BIOS.")
            dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL,
                            "Close _Without Applying", Gtk.ResponseType.CLOSE)
            response = dlg.run()
            dlg.destroy()
            if response != Gtk.ResponseType.CLOSE:
                return True
        if self.helper:
            self.helper.close()
            self.helper = None
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    GLib.set_prgname("dell-bios-gtk")
    GLib.set_application_name(APP_TITLE)
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
