"""GTK4 / libadwaita window: one tab per step, each tab shows its own status."""

import copy
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import checks, deploy, host, model, netsetup, smbios, xmlgen  # noqa: E402

APP_ID = "local.passthru.Studio"

TABS = [
    ("check", "Check"), ("gpu", "GPU"), ("os", "OS"), ("drive", "Drive"), ("cpu", "CPU"), ("ram", "RAM"),
    ("audio", "Audio"), ("network", "Network"), ("peripheral", "Peripheral"), ("preset", "Preset"), ("save", "Save"),
]
STATUS_ICON = {
    checks.OK: ("object-select-symbolic", "st-ok"),
    checks.WARN: ("dialog-warning-symbolic", "st-warn"),
    checks.ERR: ("dialog-error-symbolic", "st-error"),
}

CSS = """
:root {
  --accent-bg-color: #8b5cf6; --accent-color: #a78bfa; --accent-fg-color: #ffffff;
  --window-bg-color: #0a0a0b; --window-fg-color: #e7e7ea;
  --view-bg-color: #0a0a0b; --headerbar-bg-color: #0a0a0b; --headerbar-shade-color: transparent;
  --card-bg-color: #131316; --card-shade-color: #1d1d22;
  --popover-bg-color: #17171b; --dialog-bg-color: #131316; --sidebar-bg-color: #0a0a0b;
}
.tabbar { padding: 0 20px; border-bottom: 1px solid #1c1c21; }
.tab { background: none; box-shadow: none; border-radius: 0; padding: 14px 10px 0 10px; min-height: 0;
       color: #8e8e98; font-weight: 500; }
.tab:hover { color: #e7e7ea; background: none; }
.tab.active { color: #b49cff; }
.tab .bar { min-height: 4px; border-radius: 4px 4px 0 0; margin-top: 12px; background: transparent; }
.tab.active .bar { background: #8b5cf6; }
.st-ok { color: #a78bfa; } .st-warn { color: #f5a524; } .st-error { color: #f05252; }
.page-title { font-size: 22px; font-weight: 700; }
.page-sub { color: #8e8e98; }
.choice { padding: 14px 18px; border-radius: 12px; background: #131316; border: 1px solid #1f1f25; }
.choice:hover { background: #18181d; }
.choice:checked { background: alpha(#8b5cf6, 0.16); border-color: #8b5cf6; color: #f1ecff; }
.choice:disabled { opacity: 0.38; }
.choice .big { font-size: 20px; font-weight: 700; }
.choice .small { font-size: 11px; color: #8e8e98; }
.chip { padding: 4px 9px; border-radius: 8px; background: #17171b; font-family: monospace; font-size: 12px; }
.chip.vm { background: alpha(#8b5cf6, 0.28); color: #e9e2ff; }
.chip.host { background: #1c1c21; color: #9a9aa3; }
.dimm { padding: 10px 6px; border-radius: 6px; background: #17171b; min-width: 84px; }
.dimm.full { background: alpha(#8b5cf6, 0.22); }
.mono { font-family: monospace; font-size: 12px; }
.footer { padding: 10px 20px; border-top: 1px solid #1c1c21; }
"""


class CheckBox:
    """A preferences group plus the check rows currently shown in it."""

    def __init__(self, group):
        self.group = group
        self.rows = []


def _row_status(level):
    icon, cls = STATUS_ICON[level]
    return Gtk.Image(icon_name=icon, css_classes=[cls])


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Passthru", default_width=1080, default_height=760)
        self.h = checks.HostInfo()
        self.cfg = model.load_last() or self.default_config()
        self.cfg.ensure_ids()
        self._normalize_for_host()
        self.verify_results = None
        self._host_cache = (None, [])
        self._status_pending = False
        self.current = "check"
        self.tab_check_groups = {}
        self.last_checks = []

        self.toast = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Passthru", subtitle="single-GPU passthrough"))
        tv.add_top_bar(header)
        tv.add_top_bar(self._tabbar())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        tv.set_content(self.stack)
        tv.add_bottom_bar(self._footer())
        self.toast.set_child(tv)
        self.set_content(self.toast)

        self.builders = {k: getattr(self, "page_" + k) for k, _ in TABS}
        for key, _ in TABS:
            self.stack.add_named(self._wrap(key), key)
        self.select(self.current)
        self.update_status()
        for t in getattr(self, "_pending_toasts", []):
            self.notify(t)
        self._pending_toasts = []
        self.connect("close-request", lambda *_: model.save_last(self.cfg) or False)

    # ---- defaults ------------------------------------------------------
    def default_config(self):
        h = self.h
        cfg = model.Config()
        cfg.emulator = model.default_emulator()
        cfg.stealth = model.stealth_available()  # plain passthrough on machines without the stealth setup
        if not cfg.stealth:
            cfg.tpm = "emulated" if host.which("swtpm") else "none"
        machines = h.qemu_info(cfg.emulator)[1]
        q35 = sorted((m for m in machines if m.startswith("pc-q35-")),
                     key=lambda m: [int(x) if x.isdigit() else 0 for x in m[7:].split(".")])
        if q35:
            cfg.machine = q35[-1]
        gpus = host.gpus(h.pci)
        if gpus:
            g = next((x for x in gpus if x.boot_vga), gpus[0])
            cfg.gpu = [g.addr] + [c.addr for c in host.gpu_companions(g, h.pci)]
            cfg.unbind_console = host.gpu_driver_family(g.driver) in ("nvidia", "amd")
        fitting = [c for c, t in model.CPU_PRESETS if c < len(h.cores)]
        cfg.cores = fitting[-1] if fitting else 1
        cfg.threads = min(2, min(len(c.threads) for c in h.cores))
        cfg.ram_gib = max([r for r in model.RAM_PRESETS if model.ram_option_state(r, h.mem_mib)[0]] or [4])
        cfg.ram_gib = min(cfg.ram_gib, 16)
        for c in self.usb_controllers():
            devs = host.usb_on_controller(c.addr, h.usb)
            if devs and not host.group_problems([c.addr], h.pci) and not host.mounted_disks_on(c.addr):
                cfg.usb_controllers.append(c.addr)
                break
        isos = [i for i in h.isos if host.guess_os(i) in ("win11", "win10")]
        if isos:
            cfg.iso = isos[0]
            cfg.os = host.guess_os(isos[0])
        virtio = [i for i in h.isos if host.guess_os(i) == "virtio"]
        cfg.virtio_iso = virtio[0] if virtio else ""
        n, base = 1, cfg.os if cfg.os in ("win10", "win11") else "vm"
        cfg.name = base + "-gpu"
        while cfg.name in h.domains:
            n += 1
            cfg.name = f"{base}-gpu{n}"
        return cfg

    def _normalize_for_host(self):
        """A config saved on another machine (or before QEMU moved) must not point at
        a missing emulator or ask for masking this host can't provide."""
        if not os.access(self.cfg.emulator, os.X_OK):
            self.cfg.emulator = model.default_emulator()
        if self.cfg.stealth and not model.stealth_available():
            self.cfg.stealth = False
            self.notify("Маскировка выключена: на этом компьютере нет её QEMU и SMBIOS-блобов")
        if self.cfg.tpm == "passthrough" and not os.path.exists("/dev/tpm0"):
            self.cfg.tpm = "emulated" if host.which("swtpm") else "none"

    def usb_controllers(self):
        return [d for d in self.h.pci if d.cls >> 8 == 0x0C03]

    # ---- chrome --------------------------------------------------------
    def _tabbar(self):
        box = Gtk.Box(css_classes=["tabbar"], spacing=6)
        self.tab_buttons, self.tab_icons = {}, {}
        for key, title in TABS:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            row = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            row.append(Gtk.Label(label=title))
            icon = Gtk.Image(pixel_size=14)
            row.append(icon)
            inner.append(row)
            inner.append(Gtk.Box(css_classes=["bar"]))
            b = Gtk.Button(child=inner, css_classes=["tab", "flat"])
            b.connect("clicked", lambda _b, k=key: self.select(k))
            box.append(b)
            self.tab_buttons[key], self.tab_icons[key] = b, icon
        return box

    def _footer(self):
        bar = Gtk.Box(css_classes=["footer"], spacing=8)
        self.footer_label = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label"],
                                      ellipsize=Pango.EllipsizeMode.END)
        self.back_btn = Gtk.Button(label="Назад")
        self.next_btn = Gtk.Button(label="Далее", css_classes=["suggested-action"])
        self.back_btn.connect("clicked", lambda *_: self.step(-1))
        self.next_btn.connect("clicked", lambda *_: self.step(1))
        for w in (self.footer_label, self.back_btn, self.next_btn):
            bar.append(w)
        return bar

    def step(self, d):
        keys = [k for k, _ in TABS]
        i = keys.index(self.current) + d
        if 0 <= i < len(keys):
            self.select(keys[i])

    def select(self, key):
        self.current = key
        self.stack.set_visible_child_name(key)
        for k, b in self.tab_buttons.items():
            (b.add_css_class if k == key else b.remove_css_class)("active")
        keys = [k for k, _ in TABS]
        self.back_btn.set_sensitive(key != keys[0])
        self.next_btn.set_visible(key != keys[-1])

    def _wrap(self, key):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=24, margin_bottom=32,
                       margin_start=16, margin_end=16)
        self.builders[key](page)
        clamp = Adw.Clamp(maximum_size=860, child=page)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def rebuild(self, *keys):
        def do():
            for key in keys:
                old = self.stack.get_child_by_name(key)
                adj = old.get_vadjustment().get_value() if old else 0
                new = self._wrap(key)
                self.stack.remove(old)
                self.stack.add_named(new, key)
                GLib.idle_add(lambda n=new, a=adj: n.get_vadjustment().set_value(a) or False)
            self.stack.set_visible_child_name(self.current)
            return False
        GLib.idle_add(do)

    def rebuild_all(self):
        self.rebuild(*[k for k, _ in TABS])

    def to_clipboard(self, text):
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))

    def notify(self, text):
        toast = getattr(self, "toast", None)
        if toast is None:  # called during __init__ before the UI exists
            self._pending_toasts = getattr(self, "_pending_toasts", [])
            self._pending_toasts.append(text)
            return
        toast.add_toast(Adw.Toast(title=text, timeout=4))

    # ---- status --------------------------------------------------------
    def changed(self, *rebuild):
        """Call after any cfg change; optionally rebuild some pages."""
        self.verify_results = None
        if rebuild:
            self.rebuild(*rebuild)
        if not self._status_pending:
            self._status_pending = True
            GLib.timeout_add(150, self._status_tick)

    def _status_tick(self):
        self._status_pending = False
        self.update_status()
        model.save_last(self.cfg)
        return False

    def host_checks(self):
        key = (self.cfg.emulator, self.cfg.machine, self.cfg.secure_boot, self.cfg.stealth, id(self.h))
        if self._host_cache[0] != key:
            self._host_cache = (key, checks.host_checks(self.h, self.cfg))
        return self._host_cache[1]

    def all_checks(self):
        return self.host_checks() + checks.config_checks(self.h, self.cfg)

    def update_status(self):
        res = self.all_checks()
        self.last_checks = res
        for key, _ in TABS:
            items = [c for c in res if c.tab == key]
            if key == "save" and self.verify_results is not None:
                items += self.verify_results
            icon = self.tab_icons[key]
            for cls in ("st-ok", "st-warn", "st-error"):
                icon.remove_css_class(cls)
            if not items:
                icon.set_visible(False)
                continue
            name, cls = STATUS_ICON[checks.worst(items)]
            icon.set_from_icon_name(name)
            icon.add_css_class(cls)
            icon.set_visible(True)
        errors = [c for c in res if c.level == checks.ERR]
        warns = [c for c in res if c.level == checks.WARN]
        if errors:
            self.footer_label.set_text(f"Ошибок: {len(errors)} — первая: {errors[0].title}")
        elif warns:
            self.footer_label.set_text(f"Готово к созданию, предупреждений: {len(warns)}")
        else:
            self.footer_label.set_text("Всё готово к созданию")
        self.refresh_tab_checks()

    def refresh_tab_checks(self):
        for key, box in list(self.tab_check_groups.items()):
            if box.group.get_root() is not None:
                self._fill_checks(box, [c for c in self.last_checks if c.tab == key])

    # ---- small widget helpers -----------------------------------------
    def header(self, page, title, sub=""):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["page-title"]))
        if sub:
            box.append(Gtk.Label(label=sub, xalign=0, wrap=True, css_classes=["page-sub"]))
        page.append(box)

    def checks_group(self, page, key, title="Проверки"):
        box = CheckBox(Adw.PreferencesGroup(title=title))
        page.append(box.group)
        self.tab_check_groups[key] = box
        self._fill_checks(box, [c for c in self.last_checks if c.tab == key])
        return box.group

    def _fill_checks(self, box, items):
        for r in box.rows:
            box.group.remove(r)
        box.rows = []
        box.group.set_visible(bool(items))
        for c in items:
            r = Adw.ActionRow(title=GLib.markup_escape_text(c.title), subtitle=GLib.markup_escape_text(c.detail),
                              subtitle_lines=3)
            r.add_prefix(_row_status(c.level))
            box.group.add(r)
            box.rows.append(r)

    def switch(self, group, title, sub, attr, rebuild=(), on_change=None):
        r = Adw.SwitchRow(title=title, subtitle=sub, active=getattr(self.cfg, attr))

        def cb(row, _p):
            setattr(self.cfg, attr, row.get_active())
            if on_change:
                on_change()
            self.changed(*rebuild)
        r.connect("notify::active", cb)
        group.add(r)
        return r

    def combo(self, group, title, items, current, on_pick, sub=""):
        """items: list of (value, label)."""
        labels = Gtk.StringList.new([l for _, l in items])
        r = Adw.ComboRow(title=title, subtitle=sub, model=labels)
        values = [v for v, _ in items]
        if current in values:
            r.set_selected(values.index(current))
        r.connect("notify::selected", lambda row, _p: on_pick(values[row.get_selected()])
                  if row.get_selected() < len(values) else None)
        group.add(r)
        return r

    def toggle_group(self, items, current, on_pick):
        tg = Adw.ToggleGroup(homogeneous=True)
        for v, l in items:
            tg.add(Adw.Toggle(label=l, name=v))
        tg.set_active_name(current)
        tg.connect("notify::active-name", lambda t, _p: on_pick(t.get_active_name()))
        return tg

    def choice_grid(self, page, options, is_active, on_pick):
        """options: list of (key, big, small, enabled, tooltip)."""
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=10,
                           row_spacing=10, max_children_per_line=len(options),
                           min_children_per_line=min(len(options), 6))
        first = None
        for key, big, small, enabled, tip in options:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.append(Gtk.Label(label=big, css_classes=["big"]))
            inner.append(Gtk.Label(label=small, css_classes=["small"], wrap=True, justify=Gtk.Justification.CENTER))
            b = Gtk.ToggleButton(child=inner, css_classes=["choice"], sensitive=enabled, active=is_active(key))
            if tip:
                b.set_tooltip_text(tip)
            if first:
                b.set_group(first)
            else:
                first = b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and on_pick(k))
            flow.append(b)
        page.append(flow)
        return flow

    def file_button(self, title, on_file, filters=None):
        b = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                       tooltip_text="Выбрать файл")

        def pick(*_):
            dlg = Gtk.FileDialog(title=title)
            if filters:
                store = Gio.ListStore.new(Gtk.FileFilter)
                f = Gtk.FileFilter(name=filters[0])
                for pat in filters[1]:
                    f.add_pattern(pat)
                store.append(f)
                dlg.set_filters(store)

            def done(d, res):
                try:
                    gf = d.open_finish(res)
                except GLib.Error:
                    return
                if gf and gf.get_path():
                    on_file(gf.get_path())
            dlg.open(self, None, done)
        b.connect("clicked", pick)
        return b

    # ---- pages ---------------------------------------------------------
    def page_check(self, page):
        self.header(page, "Проверка системы",
                    "Всё, что нужно для single-GPU passthrough на этом компьютере. Ничего не запускается.")
        g = Adw.PreferencesGroup(title="QEMU")
        page.append(g)
        e = Adw.EntryRow(title="Эмулятор (свой QEMU)", text=self.cfg.emulator, show_apply_button=True)

        def set_emu(row):
            self.cfg.emulator = row.get_text().strip()
            self.changed("check")
        e.connect("apply", set_emu)
        e.add_suffix(self.file_button("QEMU", lambda p: (setattr(self.cfg, "emulator", p), self.changed("check"))))
        g.add(e)
        _, machines, _ = self.h.qemu_info(self.cfg.emulator)
        q35 = sorted((m for m in machines if m.startswith("pc-q35-")),
                     key=lambda m: [int(x) if x.isdigit() else 0 for x in m[7:].split(".")], reverse=True)
        if self.cfg.machine not in q35:
            q35.insert(0, self.cfg.machine)
        self.combo(g, "Тип машины", [(m, m) for m in q35[:12]], self.cfg.machine,
                   lambda v: (setattr(self.cfg, "machine", v), self.changed()))

        hg = Adw.PreferencesGroup(title="Хост")
        page.append(hg)
        cores = self.h.cores
        hg.add(Adw.ActionRow(title="Процессор", subtitle=f"{self.h.cpu_name} — {len(cores)} ядер / "
                             f"{sum(len(c.threads) for c in cores)} потоков"))
        hg.add(Adw.ActionRow(title="Память", subtitle=f"{self.h.mem_mib / 1024:.1f} ГиБ"))
        gpus = host.gpus(self.h.pci)
        hg.add(Adw.ActionRow(title="Видеокарты", subtitle="\n".join(f"{g.short} {g.name} ({g.driver})" for g in gpus)
                             or "нет", subtitle_lines=4))
        cg = self.checks_group(page, "check")
        again = Gtk.Button(label="Перепроверить", css_classes=["flat"])

        def redo(*_):
            self.h.refresh()
            self._host_cache = (None, [])
            self.changed(*[k for k, _ in TABS])
            self.notify("Система перечитана")
        again.connect("clicked", redo)
        cg.set_header_suffix(again)

    def page_gpu(self, page):
        self.header(page, "Видеокарта",
                    "Карта уходит в VM целиком. На время работы VM хост остаётся без экрана: "
                    "дисплей-менеджер останавливается и все открытые программы закрываются.")
        g = Adw.PreferencesGroup(title="Какую карту отдать")
        page.append(g)
        gpus = host.gpus(self.h.pci)
        first = None
        for gpu in gpus:
            r = Adw.ActionRow(title=GLib.markup_escape_text(gpu.name), activatable=True,
                              subtitle=f"{gpu.short} · {gpu.ids} · драйвер {gpu.driver or '—'} · IOMMU {gpu.group}"
                              + (" · основная (boot VGA)" if gpu.boot_vga else ""))
            cb = Gtk.CheckButton(active=gpu.addr in self.cfg.gpu)
            if first:
                cb.set_group(first)
            else:
                first = cb

            def pick(btn, gpu=gpu):
                if not btn.get_active():
                    return
                self.cfg.gpu = [gpu.addr] + [c.addr for c in host.gpu_companions(gpu, self.h.pci)]
                self.cfg.unbind_console = host.gpu_driver_family(gpu.driver) == "nvidia"
                self.changed("gpu")
            cb.connect("toggled", pick)
            r.add_prefix(cb)
            r.set_activatable_widget(cb)
            g.add(r)
        if not gpus:
            g.add(Adw.ActionRow(title="Видеокарты не найдены"))

        sel = next((d for d in gpus if d.addr in self.cfg.gpu), None)
        if sel:
            cg = Adw.PreferencesGroup(title="Что отдаётся вместе с ней",
                                      description="Звук HDMI/DP и другие функции карты. "
                                                  "Всё из одной IOMMU-группы должно уйти вместе.")
            page.append(cg)
            extra = host.gpu_companions(sel, self.h.pci)
            grp = [self.h.pci_by_addr[a] for a in host.group_members(sel.group)
                   if a in self.h.pci_by_addr and a != sel.addr and not self.h.pci_by_addr[a].is_bridge]
            for d in {x.addr: x for x in extra + grp}.values():
                r = Adw.SwitchRow(title=GLib.markup_escape_text(d.name), active=d.addr in self.cfg.gpu,
                                  subtitle=f"{d.short} · {d.ids} · драйвер {d.driver or '—'} · IOMMU {d.group}")

                def tog(row, _p, a=d.addr):
                    if row.get_active() and a not in self.cfg.gpu:
                        self.cfg.gpu.append(a)
                    elif not row.get_active() and a in self.cfg.gpu:
                        self.cfg.gpu.remove(a)
                    self.changed()
                r.connect("notify::active", tog)
                cg.add(r)
            if not extra and not grp:
                cg.add(Adw.ActionRow(title="Других функций нет"))

        adv = Adw.PreferencesGroup(title="Дополнительно")
        page.append(adv)
        rom = Adw.EntryRow(title="Файл vBIOS (обычно не нужен)", text=self.cfg.gpu_rom, show_apply_button=True)
        rom.connect("apply", lambda r: (setattr(self.cfg, "gpu_rom", r.get_text().strip()), self.changed()))
        rom.add_suffix(self.file_button("vBIOS", lambda p: (setattr(self.cfg, "gpu_rom", p), self.changed("gpu")),
                                        ("ROM", ["*.rom", "*.bin"])))
        adv.add(rom)
        self.switch(adv, "Отвязать консоль и efifb", "Нужно для NVIDIA; для Intel/AMD обычно не требуется",
                    "unbind_console")
        self.checks_group(page, "gpu")

    def page_os(self, page):
        self.header(page, "Система", "Что ставим и с какого образа.")
        page.append(self.toggle_group([(k, v[0]) for k, v in model.OS_CHOICES.items()], self.cfg.os, self._set_os))
        g = Adw.PreferencesGroup(title="Установочный образ")
        page.append(g)
        isos = [i for i in self.h.isos if host.guess_os(i) != "virtio"]
        if self.cfg.iso and self.cfg.iso not in isos:
            isos.insert(0, self.cfg.iso)
        items = [("", "Без ISO (диск уже с системой)")] + [(i, os.path.basename(i)) for i in isos]
        r = self.combo(g, "ISO", items, self.cfg.iso, self._set_iso, sub=self.cfg.iso or "")
        r.add_suffix(self.file_button("ISO", lambda p: self._set_iso(p, rebuild=True), ("ISO", ["*.iso", "*.ISO"])))
        if self.cfg.os in ("win10", "win11"):
            vis = [i for i in self.h.isos if host.guess_os(i) == "virtio"]
            if self.cfg.virtio_iso and self.cfg.virtio_iso not in vis:
                vis.insert(0, self.cfg.virtio_iso)
            v = self.combo(g, "Драйверы virtio-win", [("", "Не подключать")] + [(i, os.path.basename(i)) for i in vis],
                           self.cfg.virtio_iso, lambda p: (setattr(self.cfg, "virtio_iso", p), self.changed()),
                           sub="Нужны, если диск или сеть virtio")
            v.add_suffix(self.file_button("virtio-win", lambda p: (setattr(self.cfg, "virtio_iso", p),
                                                                   self.changed("os")), ("ISO", ["*.iso"])))
        s = Adw.PreferencesGroup(title="Безопасность")
        page.append(s)
        self.combo(s, "TPM 2.0", [("passthrough", "Проброс TPM хоста (/dev/tpm0)"), ("emulated", "Эмулируемый (swtpm)"),
                                  ("none", "Нет")], self.cfg.tpm,
                   lambda v: (setattr(self.cfg, "tpm", v), self.changed()))
        self.switch(s, "Secure Boot", "OVMF с поддержкой Secure Boot (ключи не записаны)", "secure_boot")
        self.checks_group(page, "os")

    def _set_os(self, v):
        if v and v != self.cfg.os:
            self.cfg.os = v
            if v == "linux" and self.cfg.tpm == "passthrough":
                self.cfg.tpm = "none"
            self.changed("os")

    def _set_iso(self, p, rebuild=False):
        self.cfg.iso = p
        guess = host.guess_os(p) if p else ""
        if guess in ("win10", "win11", "linux") and guess != self.cfg.os:
            self.cfg.os = guess
            rebuild = True
        if p and p not in self.h.isos:
            self.h.isos.insert(0, p)
        self.changed(*(["os"] if rebuild else []))

    def page_drive(self, page):
        self.header(page, "Диск", "Куда ставится система.")
        page.append(self.toggle_group([("new", "Новый образ"), ("file", "Существующий образ"), ("block", "Целый диск")],
                                      self.cfg.disk_mode, self._set_disk_mode))
        g = Adw.PreferencesGroup()
        page.append(g)
        if self.cfg.disk_mode == "new":
            g.add(Adw.ActionRow(title="Файл", subtitle=self.cfg.default_disk + " (пул libvirt «default»)"))
            spin = Adw.SpinRow.new_with_range(32, 4096, 16)
            spin.set_title("Размер, ГБ")
            spin.set_subtitle("qcow2 занимает место по мере записи")
            spin.set_value(self.cfg.disk_size_gb)
            spin.connect("notify::value", lambda r, _p: (setattr(self.cfg, "disk_size_gb", int(r.get_value())),
                                                         self.changed()))
            box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
            for size in (64, 128, 256, 512):
                b = Gtk.Button(label=str(size), css_classes=["flat"])
                b.connect("clicked", lambda _b, s=size: spin.set_value(s))
                box.append(b)
            spin.add_suffix(box)
            g.add(spin)
        elif self.cfg.disk_mode == "file":
            vols = checks._pool_list()
            imgs = [p for n, p in vols if p.endswith((".qcow2", ".img", ".raw")) and not p.endswith(".iso")]
            if self.cfg.disk_path and self.cfg.disk_path not in imgs:
                imgs.insert(0, self.cfg.disk_path)
            r = self.combo(g, "Образ", [("", "—")] + [(p, os.path.basename(p)) for p in imgs], self.cfg.disk_path,
                           lambda p: (setattr(self.cfg, "disk_path", p), self.changed()), sub=self.cfg.disk_path)
            r.add_suffix(self.file_button("Образ диска", lambda p: (setattr(self.cfg, "disk_path", p),
                                                                    self.changed("drive")),
                                          ("Образы", ["*.qcow2", "*.img", "*.raw"])))
            used = self._disks_used_by_other_vms()
            if self.cfg.disk_path in used:
                g.add(Adw.ActionRow(title="Этот образ уже подключён к VM " + ", ".join(used[self.cfg.disk_path]),
                                    subtitle="Не запускай обе VM одновременно"))
        else:
            ids = sorted(p for p in (os.path.join("/dev/disk/by-id", n) for n in os.listdir("/dev/disk/by-id"))
                         if "-part" not in p and not os.path.basename(p).startswith(("dm-", "wwn-", "nvme-eui")))
            self.combo(g, "Устройство", [("", "—")] + [(p, os.path.basename(p)) for p in ids], self.cfg.disk_path,
                       lambda p: (setattr(self.cfg, "disk_path", p), self.changed()),
                       sub="Весь диск уходит в VM. Смонтированные диски будут отмечены ошибкой.")
        b = Adw.PreferencesGroup(title="Шина")
        page.append(b)
        self.combo(b, "Контроллер", [("sata", "SATA — как настоящий SSD (маскировка, без драйверов)"),
                                     ("virtio", "virtio — быстрее, нужен драйвер virtio-win")],
                   self.cfg.disk_bus, lambda v: (setattr(self.cfg, "disk_bus", v), self.changed()))
        self.checks_group(page, "drive")

    def _disks_used_by_other_vms(self):
        used = {}
        for d in self.h.domains:
            if d == self.cfg.name:
                continue
            rc, out = host.run(checks.VIRSH + ["domblklist", d, "--details", "--inactive"])
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 4 and parts[1] == "disk":
                    used.setdefault(parts[3], []).append(d)
        return used

    def _set_disk_mode(self, v):
        if v and v != self.cfg.disk_mode:
            self.cfg.disk_mode = v
            self.cfg.disk_path = ""
            self.changed("drive")

    def page_cpu(self, page):
        cores = self.h.cores
        nthreads = sum(len(c.threads) for c in cores)
        self.header(page, "Процессор", f"{self.h.cpu_name}: {len(cores)} ядер / {nthreads} потоков. "
                                       "Ядро 0 остаётся хосту, остальные закрепляются за VM.")
        opts = []
        for c, t in model.CPU_PRESETS:
            ok, note = model.cpu_option_state(cores, c, t)
            opts.append((f"{c}/{c * t}", f"{c}/{c * t}", "ядер / потоков" if ok else "нет у этого CPU", ok, note))
        cur = f"{self.cfg.cores}/{self.cfg.vcpus}"

        def pick(key):
            c, v = (int(x) for x in key.split("/"))
            self.cfg.cores, self.cfg.threads = c, v // c
            self.changed("cpu")
        self.choice_grid(page, opts, lambda k: k == cur, pick)

        g = Adw.PreferencesGroup(title="Своё значение")
        page.append(g)
        sc = Adw.SpinRow.new_with_range(1, max(1, len(cores)), 1)
        sc.set_title("Ядра")
        sc.set_value(self.cfg.cores)
        st = Adw.SpinRow.new_with_range(1, max(len(c.threads) for c in cores), 1)
        st.set_title("Потоков на ядро")
        st.set_value(self.cfg.threads)
        sc.connect("notify::value", lambda r, _p: self._set_cpu(int(r.get_value()), self.cfg.threads))
        st.connect("notify::value", lambda r, _p: self._set_cpu(self.cfg.cores, int(r.get_value())))
        g.add(sc)
        g.add(st)

        m = Adw.PreferencesGroup(title="Раскладка по ядрам хоста")
        page.append(m)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, min_children_per_line=4,
                           column_spacing=6, row_spacing=6, margin_top=6)
        try:
            p = model.plan_pinning(cores, self.cfg.cores, self.cfg.threads) if self.cfg.pin else None
        except ValueError:
            p = None
        for i, c in enumerate(cores):
            vm = p is not None and i in p.vm_cores
            lab = f"C{i}  {'/'.join(map(str, c.threads))}\n{'VM' if vm else 'хост'}"
            flow.append(Gtk.Label(label=lab, css_classes=["chip", "vm" if vm else "host"],
                                  justify=Gtk.Justification.CENTER))
        m.add(flow)
        t = Adw.PreferencesGroup(title="Настройка")
        page.append(t)
        self.switch(t, "Закрепить vCPU за ядрами (pinning)", "Пары гиперпотоков идут в VM вместе", "pin",
                    rebuild=("cpu",))
        self.switch(t, "Убрать службы хоста с ядер VM", "cgroups AllowedCPUs на время работы VM", "isolate")
        self.switch(t, "Реалтайм-приоритет vCPU (FIFO)", "Меньше задержек; при всех ядрах отключается сам", "fifo")
        self.switch(t, "Governor performance на время VM", "", "perf_hooks")
        self.checks_group(page, "cpu")

    def _set_cpu(self, c, t):
        if (c, t) != (self.cfg.cores, self.cfg.threads):
            self.cfg.cores, self.cfg.threads = c, t
            self.changed("cpu")

    def page_ram(self, page):
        self.header(page, "Память", f"У хоста {self.h.mem_mib / 1024:.1f} ГиБ. "
                                    f"Хосту оставляется минимум {model.HOST_RAM_RESERVE_MIB // 1024} ГиБ.")
        opts = []
        for r in model.RAM_PRESETS:
            ok, note = model.ram_option_state(r, self.h.mem_mib)
            opts.append((r, f"{r} GB", model.dimm_text(r) if ok else "больше, чем можно", ok, note))

        def pick(r):
            if r != self.cfg.ram_gib:
                self.cfg.ram_gib = r
                self.changed("ram")
        self.choice_grid(page, opts, lambda k: k == self.cfg.ram_gib, pick)
        g = Adw.PreferencesGroup(title="Своё значение")
        page.append(g)
        sp = Adw.SpinRow.new_with_range(2, max(2, model.ram_limit_gib(self.h.mem_mib)), 1)
        sp.set_title("ГБ")
        sp.set_value(self.cfg.ram_gib)
        sp.connect("notify::value", lambda r, _p: pick(int(r.get_value())))
        g.add(sp)

        if self.cfg.stealth:
            d = Adw.PreferencesGroup(title="Планки, которые увидит Windows",
                                     description="Блобы SMBIOS type 16/17/6 генерируются под объём — "
                                                 "сумма планок всегда равна памяти VM.")
            page.append(d)
            lay = smbios.layout_for(self.cfg.ram_gib)
            row = Gtk.Box(spacing=8, margin_top=6, homogeneous=True)
            for i, slot in enumerate(smbios.SLOTS):
                size = lay[i] if lay else None
                lab = f"DIMM_{slot}\n{size} ГБ" if size else f"DIMM_{slot}\nпусто"
                row.append(Gtk.Label(label=lab, justify=Gtk.Justification.CENTER,
                                     css_classes=["dimm"] + (["full"] if size else [])))
            d.add(row)
        h = Adw.PreferencesGroup()
        page.append(h)
        self.switch(h, "Hugepages 2 МиБ", "Память VM выделяется большими страницами при запуске и отдаётся после",
                    "hugepages")
        self.checks_group(page, "ram")

    def page_audio(self, page):
        self.header(page, "Звук", "Звук HDMI/DP уходит вместе с видеокартой. "
                                  "Можно дополнительно отдать встроенный звук материнки.")
        page.append(self.toggle_group([("none", "Только видеокарта"), ("host", "Отдать звуковой контроллер")],
                                      self.cfg.audio, lambda v: v and (setattr(self.cfg, "audio", v),
                                                                       self.changed("audio"))))
        if self.cfg.audio == "host":
            g = Adw.PreferencesGroup(title="Контроллеры")
            page.append(g)
            auds = [d for d in self.h.pci if d.base_class == 0x04 and d.addr not in self.cfg.gpu]
            for d in auds:
                r = Adw.SwitchRow(title=GLib.markup_escape_text(d.name), active=d.addr in self.cfg.audio_pci,
                                  subtitle=f"{d.short} · драйвер {d.driver or '—'} · IOMMU {d.group}")

                def tog(row, _p, a=d.addr):
                    lst = self.cfg.audio_pci
                    if row.get_active() and a not in lst:
                        lst.append(a)
                    elif not row.get_active() and a in lst:
                        lst.remove(a)
                    self.changed()
                r.connect("notify::active", tog)
                g.add(r)
            if not auds:
                g.add(Adw.ActionRow(title="Других звуковых контроллеров нет"))
        self.checks_group(page, "audio")

    def page_network(self, page):
        self.header(page, "Сеть", "NAT работает сразу. Мост нужен, если VM должна быть видна в локальной сети.")
        g = Adw.PreferencesGroup()
        page.append(g)
        modes = [("nat", "NAT (libvirt)"), ("bridge", "Мост"), ("direct", "macvtap на сетевую карту"), ("none", "Без сети")]

        def set_mode(v):
            if v == self.cfg.net:
                return
            self.cfg.net = v
            self.cfg.net_source = {"nat": "default" if "default" in self.h.networks else
                                   (self.h.networks or [""])[0],
                                   "bridge": (self.h.bridges or [""])[0], "direct": (self.h.nics or [""])[0],
                                   "none": ""}[v]
            self.changed("network")
        self.combo(g, "Режим", modes, self.cfg.net, set_mode)
        src = {"nat": self.h.networks, "bridge": self.h.bridges, "direct": self.h.nics}.get(self.cfg.net)
        if src is not None:
            self.combo(g, "Источник", [(s, s) for s in src] or [("", "нет")], self.cfg.net_source,
                       lambda v: (setattr(self.cfg, "net_source", v), self.changed()))
        if self.cfg.net != "none":
            def set_model(v):
                if v != self.cfg.net_model:
                    self.cfg.net_model = v
                    self.cfg.mac = model.random_mac(v)
                    self.changed("network")
            self.combo(g, "Модель карты", list(model.NET_MODELS.items()), self.cfg.net_model, set_model)
            mac = Adw.EntryRow(title="MAC", text=self.cfg.mac, show_apply_button=True)
            mac.connect("apply", lambda r: (setattr(self.cfg, "mac", r.get_text().strip().lower()), self.changed()))
            regen = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text="Новый MAC")
            regen.connect("clicked", lambda *_: (setattr(self.cfg, "mac", model.random_mac(self.cfg.net_model)),
                                                 self.changed("network")))
            mac.add_suffix(regen)
            g.add(mac)

        if self.cfg.net in ("nat", "bridge"):
            fw = Adw.PreferencesGroup(title="Интернет и firewall",
                                      description="libvirt ставит NAT сам, но ufw/firewalld часто режут форвардинг "
                                                  "моста, и в VM нет интернета. Кнопка чинит это (через sudo).")
            page.append(fw)
            lvl, title, detail = netsetup.status(self.cfg)
            r = Adw.ActionRow(title=title, subtitle=detail or netsetup.active_firewall(), subtitle_lines=2)
            r.add_prefix(_row_status(lvl))
            btn = Gtk.Button(label="Настроить (sudo)", valign=Gtk.Align.CENTER,
                             sensitive=netsetup.needed(self.cfg))
            btn.connect("clicked", lambda *_: self.on_setup_network())
            r.add_suffix(btn)
            sh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Проверить снова")
            sh.connect("clicked", lambda *_: self.changed("network"))
            r.add_suffix(sh)
            fw.add(r)

        self.checks_group(page, "network")

    def on_setup_network(self):
        path = netsetup.script_path(self.cfg)
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w") as f:
            f.write(netsetup.script(self.cfg))
        os.chmod(path, 0o755)
        if deploy.open_terminal(f"sudo bash {path}"):
            self.notify("Открыт терминал: введи пароль sudo, затем нажми ⟳")
        else:
            self.to_clipboard(f"sudo bash {path}")
            self.notify("Терминал не найден — команда скопирована")

    def page_peripheral(self, page):
        self.header(page, "Клавиатура, мышь, USB",
                    "Хост без видеокарты остаётся без экрана, поэтому ввод должен уйти в VM. "
                    "Надёжнее всего отдать USB-контроллер целиком — тогда работает горячее подключение.")
        g = Adw.PreferencesGroup(title="USB-контроллеры (PCI)")
        page.append(g)
        for c in self.usb_controllers():
            devs = host.usb_on_controller(c.addr, self.h.usb)
            mounted = host.mounted_disks_on(c.addr)
            probs = host.group_problems([c.addr], self.h.pci)
            sub = f"{c.short} · IOMMU {c.group} · " + (", ".join(u.name for u in devs) or "пусто")
            if mounted:
                sub += f"\n⚠ смонтирован диск: {', '.join(mounted)}"
            if probs:
                sub += f"\n⚠ делит IOMMU-группу: {', '.join(a for a, _ in probs)}"
            r = Adw.SwitchRow(title=GLib.markup_escape_text(c.name), subtitle=GLib.markup_escape_text(sub),
                              active=c.addr in self.cfg.usb_controllers, subtitle_lines=4)

            def tog(row, _p, a=c.addr):
                lst = self.cfg.usb_controllers
                if row.get_active() and a not in lst:
                    lst.append(a)
                elif not row.get_active() and a in lst:
                    lst.remove(a)
                self.changed("peripheral")
            r.connect("notify::active", tog)
            g.add(r)

        u = Adw.PreferencesGroup(title="Отдельные USB-устройства",
                                 description="По vendor:product. Если устройство меняет ID (бывает у приёмников мышей), "
                                             "оно отвалится — тогда лучше контроллер.")
        page.append(u)
        on_ctrl = {x.path for a in self.cfg.usb_controllers for x in host.usb_on_controller(a, self.h.usb)}
        for d in self.h.usb:
            if d.cls == "09":
                continue
            r = Adw.SwitchRow(title=GLib.markup_escape_text(d.name), active=d.ids in self.cfg.usb_devices,
                              subtitle=f"{d.ids} · шина {d.bus}" + (" · уже на отданном контроллере"
                                                                     if d.path in on_ctrl else ""))
            r.set_sensitive(d.path not in on_ctrl or d.ids in self.cfg.usb_devices)

            def tog(row, _p, ids=d.ids):
                lst = self.cfg.usb_devices
                if row.get_active() and ids not in lst:
                    lst.append(ids)
                elif not row.get_active() and ids in lst:
                    lst.remove(ids)
                self.changed()
            r.connect("notify::active", tog)
            u.add(r)
        self.checks_group(page, "peripheral")

    def page_preset(self, page):
        self.header(page, "Пресеты", "Готовые наборы настроек. Видеокарта и USB при применении не меняются.")
        g = Adw.PreferencesGroup(title="Готовые")
        page.append(g)
        for title in list(model.BUILTIN_PRESETS) + model.user_presets():
            r = Adw.ActionRow(title=GLib.markup_escape_text(title),
                              subtitle="встроенный" if title in model.BUILTIN_PRESETS else "твой")
            b = Gtk.Button(label="Применить", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, t=title: self.apply_preset(t))
            r.add_suffix(b)
            if title not in model.BUILTIN_PRESETS:
                d = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
                d.connect("clicked", lambda _b, t=title: self.delete_preset(t))
                r.add_suffix(d)
            g.add(r)
        s = Adw.PreferencesGroup(title="Сохранить текущие настройки")
        page.append(s)
        e = Adw.EntryRow(title="Название пресета", show_apply_button=True)

        def save(row):
            t = row.get_text().strip()
            if t:
                model.save_preset(self.cfg, t)
                self.notify(f"Пресет «{t}» сохранён")
                self.changed("preset")
        e.connect("apply", save)
        s.add(e)
        m = Adw.PreferencesGroup(title="Режим")
        page.append(m)
        if model.stealth_available():
            self.switch(m, "Маскировка (stealth)",
                        "SMBIOS-идентичность хоста, планки памяти, kvm hidden, без бита hypervisor. "
                        "Выключено — Hyper-V enlightenments, быстрее, но VM видна.",
                        "stealth", rebuild=("ram", "preset"))
        else:
            r = Adw.ActionRow(title="Маскировка недоступна",
                              subtitle="Нужен кастомный QEMU (/usr/local/bin) и SMBIOS-блобы хоста. "
                                       "Без них — обычный проброс с Hyper-V enlightenments.", subtitle_lines=3)
            r.add_prefix(_row_status(checks.OK))
            m.add(r)
        self.checks_group(page, "preset")

    def apply_preset(self, title):
        self.cfg = model.apply_preset(self.cfg, title)
        self.notify(f"Применён «{title}»")
        self.changed(*[k for k, _ in TABS])

    def delete_preset(self, title):
        try:
            os.remove(os.path.join(model.presets_dir(), title + ".json"))
        except OSError:
            pass
        self.changed("preset")

    def page_save(self, page):
        self.header(page, "Сохранение", "Проверка идёт без запуска VM: схема libvirt, командная строка QEMU, "
                                        "SMBIOS-блобы и хуки в режиме dry-run.")
        g = Adw.PreferencesGroup()
        page.append(g)
        name = Adw.EntryRow(title="Имя VM", text=self.cfg.name, show_apply_button=True)

        def set_name(row):
            self.cfg.name = row.get_text().strip()
            self.changed("save", "drive")
        name.connect("apply", set_name)
        g.add(name)
        c = self.cfg
        gpu = next((self.h.pci_by_addr[a].name for a in c.gpu if a in self.h.pci_by_addr
                    and self.h.pci_by_addr[a].base_class == 0x03), "—")
        summary = [
            ("Видеокарта", f"{gpu} (+{len(c.gpu) - 1})"),
            ("Система", f"{model.OS_CHOICES[c.os][0]} · {os.path.basename(c.iso) or 'без ISO'}"),
            ("Диск", f"{c.disk} · {c.disk_bus}" + (f" · {c.disk_size_gb} ГБ" if c.disk_mode == "new" else "")),
            ("CPU / RAM", f"{c.cores}/{c.vcpus} · {c.ram_gib} ГБ" + (" · hugepages" if c.hugepages else "")),
            ("USB", ", ".join([a[5:] for a in c.usb_controllers] + c.usb_devices) or "ничего"),
            ("Сеть", "нет" if c.net == "none" else f"{c.net} {c.net_source} · {c.net_model} · {c.mac}"),
            ("Режим", "маскировка" if c.stealth else "Hyper-V"),
        ]

        bar = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        self.verify_btn = Gtk.Button(label="Проверить без запуска")
        self.verify_btn.connect("clicked", self.on_verify)
        xml_btn = Gtk.Button(label="Показать XML")
        xml_btn.connect("clicked", self.on_show_xml)
        self.create_btn = Gtk.Button(label="Создать VM", css_classes=["suggested-action"])
        self.create_btn.connect("clicked", self.on_create)
        for b in (self.verify_btn, xml_btn, self.create_btn):
            bar.append(b)
        page.append(bar)

        hk = Adw.PreferencesGroup(title="Хуки single-GPU",
                                  description="Останавливают дисплей-менеджер перед запуском и возвращают его после. "
                                              "Ставятся в /etc, поэтому через sudo.")
        page.append(hk)
        state = deploy.hooks_state(self.h, self.cfg) if model.valid_name(c.name) else "unknown"
        text = {"ok": ("Установлены и актуальны", checks.OK), "missing": ("Не установлены", checks.WARN),
                "outdated": ("Установлены, но устарели — переустанови", checks.WARN),
                "unknown": ("Не удалось прочитать", checks.WARN)}[state]
        r = Adw.ActionRow(title=text[0], subtitle=deploy.install_command(c), subtitle_selectable=True)
        r.add_prefix(_row_status(text[1]))
        bundle_ready = os.path.exists(os.path.join(c.bundle_dir, "install-hooks.sh"))
        inst = Gtk.Button(label="Установить (sudo)", valign=Gtk.Align.CENTER, sensitive=bundle_ready,
                          tooltip_text=None if bundle_ready else "Сначала «Создать VM»")
        inst.connect("clicked", self.on_install_hooks)
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                          tooltip_text="Скопировать команду")
        copy_btn.connect("clicked", lambda *_: (self.to_clipboard(deploy.install_command(c)),
                                               self.notify("Команда скопирована")))
        recheck = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                             tooltip_text="Проверить снова")
        recheck.connect("clicked", lambda *_: self.changed("save"))
        for w in (inst, copy_btn, recheck):
            r.add_suffix(w)
        hk.add(r)
        if os.path.isdir(c.bundle_dir):
            fr = Adw.ActionRow(title="Папка VM", subtitle=c.bundle_dir, activatable=True)
            fr.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
            fr.connect("activated", lambda *_: Gtk.FileLauncher.new(Gio.File.new_for_path(c.bundle_dir)).launch(
                self, None, None))
            hk.add(fr)

        self.checks_group(page, "save", "Перед сохранением")
        if self.verify_results is not None:
            vg = CheckBox(Adw.PreferencesGroup(title="Результат проверки"))
            page.append(vg.group)
            self._fill_checks(vg, self.verify_results)
        sg = Adw.PreferencesGroup(title="Итог")
        page.append(sg)
        for t, v in summary:
            sg.add(Adw.ActionRow(title=t, subtitle=GLib.markup_escape_text(v)))

    def on_verify(self, *_):
        self.verify_btn.set_sensitive(False)
        self.verify_btn.set_label("Проверяю…")
        cfg = copy.deepcopy(self.cfg)

        def work():
            res = checks.verify(self.h, cfg)
            GLib.idle_add(self._verify_done, res)
        threading.Thread(target=work, daemon=True).start()

    def _verify_done(self, res):
        self.verify_results = res
        bad = [r for r in res if r.level == checks.ERR]
        self.notify("Проверка пройдена" if not bad else f"Ошибок: {len(bad)}")
        self.update_status()
        self.rebuild("save")
        return False

    def on_show_xml(self, *_):
        try:
            xml = xmlgen.build(self.cfg, self.h.cores)
        except Exception as e:  # noqa: BLE001
            xml = f"Не собирается: {e}"
        tv = Gtk.TextView(editable=False, monospace=True, top_margin=12, left_margin=12, bottom_margin=12)
        tv.get_buffer().set_text(xml)
        dlg = Adw.Dialog(title="domain.xml", content_width=900, content_height=700)
        t = Adw.ToolbarView()
        t.add_top_bar(Adw.HeaderBar())
        t.set_content(Gtk.ScrolledWindow(child=tv))
        dlg.set_child(t)
        dlg.present(self)

    def on_create(self, *_):
        errs = [c for c in self.all_checks() if c.level == checks.ERR]
        if errs:
            d = Adw.AlertDialog(heading="Есть ошибки", body="\n".join("• " + e.title for e in errs[:8]))
            d.add_response("ok", "Понятно")
            d.present(self)
            return
        self.create_btn.set_sensitive(False)
        self.create_btn.set_label("Создаю…")
        cfg = copy.deepcopy(self.cfg)

        def work():
            steps = []
            try:
                res = checks.verify(self.h, cfg)
                bad = [r for r in res if r.level == checks.ERR]
                if bad:
                    GLib.idle_add(self._create_done, False, "Проверка не прошла: " + bad[0].title, res)
                    return
                d, _ = deploy.write_bundle(self.h, cfg)
                steps.append(f"файлы: {d}")
                ok, msg = deploy.create_disk(cfg)
                steps.append(msg)
                if not ok:
                    GLib.idle_add(self._create_done, False, "Диск: " + msg, res)
                    return
                ok, msg = deploy.define(cfg)
                if not ok:
                    GLib.idle_add(self._create_done, False, "virsh define: " + msg, res)
                    return
                GLib.idle_add(self._create_done, True, f"VM «{cfg.name}» создана. Теперь поставь хуки.", res)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._create_done, False, f"{type(e).__name__}: {e}", None)
        threading.Thread(target=work, daemon=True).start()

    def _create_done(self, ok, msg, res):
        self.verify_results = res
        self.h.domains = host.libvirt_domains()
        d = Adw.AlertDialog(heading="Готово" if ok else "Не получилось", body=msg)
        d.add_response("ok", "OK")
        if ok:
            d.add_response("hooks", "Установить хуки")
            d.set_response_appearance("hooks", Adw.ResponseAppearance.SUGGESTED)
            d.connect("response", lambda _d, r: r == "hooks" and self.on_install_hooks())
        d.present(self)
        self.update_status()
        self.rebuild("save")
        return False

    def on_install_hooks(self, *_):
        if deploy.open_terminal(deploy.install_command(self.cfg)):
            self.notify("Открыт терминал: введи пароль sudo, затем нажми ⟳")
        else:
            self.to_clipboard(deploy.install_command(self.cfg))
            self.notify("Терминал не найден — команда скопирована")


class App(Adw.Application):
    def __init__(self, app_id=APP_ID):
        super().__init__(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()
        GLib.idle_add(lambda: win.set_focus(None) or False)


def main(argv=None):
    return App().run(argv or [])
