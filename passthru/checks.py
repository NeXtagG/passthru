"""Checks shown as tab badges, plus the deep no-boot verification used by Save and --selftest."""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

from . import host, hooks, model, smbios, xmlgen

OK, WARN, ERR = "ok", "warn", "error"
RANK = {OK: 0, WARN: 1, ERR: 2}
VIRSH = ["virsh", "-c", "qemu:///system"]


@dataclass
class Check:
    tab: str
    level: str
    title: str
    detail: str = ""


class HostInfo:
    """Snapshot of the host; refresh() re-reads everything."""

    def __init__(self):
        self.refresh()

    def refresh(self):
        self.cores = host.cpu_cores()
        self.cpu_name = host.cpu_model()
        self.flags = host.cpu_flags()
        self.mem_mib = host.mem_total_mib()
        self.pci = host.pci_devices()
        self.pci_by_addr = {d.addr: d for d in self.pci}
        self.usb = host.usb_devices()
        self.dm = host.display_manager()
        self.isos = host.find_isos()
        self.domains = host.libvirt_domains()
        self.networks = host.libvirt_networks()
        self.bridges = host.bridges()
        self.nics = host.phys_nics()
        self._qemu = {}

    def qemu_info(self, emulator):
        """(version, machines, devices) of an emulator binary, cached."""
        if emulator not in self._qemu:
            rc, ver = host.run([emulator, "--version"])
            _, machines = host.run([emulator, "-machine", "help"])
            _, devs = host.run([emulator, "-device", "help"])
            self._qemu[emulator] = (ver.splitlines()[0] if rc == 0 and ver else "",
                                    set(re.findall(r"^(\S+)", machines, re.M)),
                                    set(re.findall(r'name "([^"]+)"', devs)))
        return self._qemu[emulator]


def worst(checks):
    return max((c.level for c in checks), key=RANK.get, default=OK)


# ---- host ---------------------------------------------------------------

def host_checks(h, cfg):
    out = []
    add = lambda lvl, t, d="": out.append(Check("check", lvl, t, d))

    virt = "vmx" in h.flags or "svm" in h.flags
    add(OK if virt else ERR, "Аппаратная виртуализация", "VT-x/AMD-V есть" if virt else "включи VT-x/SVM в BIOS")
    add(OK if os.access("/dev/kvm", os.R_OK | os.W_OK) else ERR, "/dev/kvm",
        "доступен" if os.path.exists("/dev/kvm") else "нет модуля kvm")
    add(OK if host.iommu_active() else ERR, "IOMMU",
        "группы есть" if host.iommu_active() else "включи VT-d в BIOS и intel_iommu=on / amd_iommu=on")
    vfio = os.path.exists("/sys/module/vfio_pci") or host.run(["modinfo", "vfio-pci"])[0] == 0
    add(OK if vfio else ERR, "Модуль vfio-pci", "есть" if vfio else "не найден")

    rc, out_v = host.run(VIRSH + ["version"])
    add(OK if rc == 0 else ERR, "libvirt (qemu:///system)",
        out_v.splitlines()[-1] if rc == 0 and out_v else out_v or "нет доступа — добавь себя в группу libvirt")

    ver, machines, devs = h.qemu_info(cfg.emulator)
    if not os.access(cfg.emulator, os.X_OK):
        add(ERR, "Свой QEMU", f"{cfg.emulator} не найден")
    else:
        add(OK, "Свой QEMU", f"{cfg.emulator}: {ver}")
        if cfg.machine not in machines:
            add(ERR, "Тип машины", f"{cfg.machine} нет в этом QEMU")
        else:
            add(OK, "Тип машины", cfg.machine)
        need = {"vfio-pci", "nec-usb-xhci", "pcie-root-port", "tpm-crb"}
        miss = need - devs
        add(OK if not miss else ERR, "Устройства QEMU", "все нужные есть" if not miss else "нет: " + ", ".join(sorted(miss)))

    code = xmlgen.OVMF_SECURE if cfg.secure_boot else xmlgen.OVMF_PLAIN
    for f in (code, "/usr/share/edk2/x64/OVMF_VARS.4m.fd"):
        add(OK if os.path.exists(f) else ERR, "OVMF " + os.path.basename(f), f)

    if os.access(hooks.DISPATCHER, os.X_OK):
        add(OK, "Диспетчер хуков libvirt", hooks.DISPATCHER)
    else:
        add(WARN, "Диспетчер хуков libvirt", "нет — install-hooks.sh поставит его и перезапустит libvirt")
    add(OK if h.dm else WARN, "Дисплей-менеджер",
        f"{h.dm} — будет остановлен на время VM" if h.dm else "не найден; без него single-GPU хук не освободит видеокарту")

    if cfg.stealth:
        miss = [f for f in xmlgen.SHARED_FILES + [a for a, _ in xmlgen.SHARED_ACPI]
                if not os.path.exists(os.path.join(xmlgen.SHARED_SMBIOS, f))]
        add(OK if not miss else WARN, "SMBIOS/ACPI-блобы X99",
            "все на месте" if not miss else "нет (VM будет без них): " + ", ".join(miss))
        home_ok = _qemu_can_enter_home()
        add(OK if home_ok else ERR, "Доступ QEMU к ~/.local/share/passthru",
            "libvirt-qemu может читать блобы" if home_ok else
            "у libvirt-qemu нет права входа в домашнюю папку: setfacl -m u:libvirt-qemu:x ~")
    return out


def _qemu_can_enter_home():
    d = model.DATA_DIR
    home = os.path.expanduser("~")
    while d.startswith(home) and d != home:
        if os.path.exists(d) and not os.stat(d).st_mode & 0o001:
            return False
        d = os.path.dirname(d)
    if os.stat(home).st_mode & 0o001:
        return True
    rc, acl = host.run(["getfacl", "-p", home])
    return bool(re.search(r"^user:libvirt-qemu:..x", acl, re.M))


# ---- per tab -----------------------------------------------------------

def config_checks(h, cfg):
    out = []
    add = lambda tab, lvl, t, d="": out.append(Check(tab, lvl, t, d))

    # GPU
    if not cfg.gpu:
        add("gpu", ERR, "Видеокарта не выбрана")
    else:
        for a in cfg.gpu:
            if a not in h.pci_by_addr:
                add("gpu", ERR, f"{a} нет в системе")
        gpus = [h.pci_by_addr[a] for a in cfg.gpu if a in h.pci_by_addr and h.pci_by_addr[a].base_class == 0x03]
        if not gpus:
            add("gpu", ERR, "В списке нет самой видеокарты")
        for g in gpus:
            add("gpu", OK, g.name, f"{g.short}, драйвер {g.driver or '—'}, IOMMU-группа {g.group}")
            fam = host.gpu_driver_family(g.driver)
            if fam == "nvidia" and not cfg.unbind_console:
                add("gpu", WARN, "NVIDIA: включи «Отвязать консоль»", "иначе efifb/vtcon держат карту")
        for a, why in host.group_problems(cfg.gpu, h.pci):
            add("gpu", ERR, f"{a}: {why}", "добавь его тоже или нужен ACS override")
        if cfg.gpu_rom and not os.path.exists(cfg.gpu_rom):
            add("gpu", ERR, "Файл vBIOS не найден", cfg.gpu_rom)

    # OS
    if cfg.iso:
        if os.path.exists(cfg.iso):
            add("os", OK, "ISO", cfg.iso)
        else:
            add("os", ERR, "ISO не найден", cfg.iso)
    else:
        add("os", WARN, "ISO не выбран", "VM загрузится только с уже установленного диска")
    if cfg.os == "win11":
        if cfg.tpm == "none":
            add("os", ERR, "Windows 11 требует TPM 2.0")
        if not cfg.secure_boot:
            add("os", WARN, "Windows 11 хочет Secure Boot")
        if cfg.disk_mode == "new" and cfg.disk_size_gb < 64:
            add("os", ERR, "Windows 11 требует диск ≥ 64 ГБ")
    if cfg.tpm == "passthrough" and not os.path.exists("/dev/tpm0"):
        add("os", ERR, "Нет /dev/tpm0 для проброса TPM", "выбери эмулируемый TPM")
    if cfg.tpm == "emulated" and not host.which("swtpm"):
        add("os", ERR, "Нет swtpm для эмулируемого TPM", "sudo pacman -S swtpm")
    if cfg.os in ("win10", "win11") and cfg.disk_bus == "virtio" and not cfg.virtio_iso:
        add("os", WARN, "Нужен virtio-win.iso", "иначе установщик Windows не увидит virtio-диск")
    if cfg.virtio_iso and not os.path.exists(cfg.virtio_iso):
        add("os", ERR, "virtio-win.iso не найден", cfg.virtio_iso)

    # Drive
    if cfg.disk_mode == "new":
        if os.path.exists(cfg.disk) or os.path.basename(cfg.disk) in _pool_volumes():
            add("drive", WARN, "Файл диска уже есть — будет использован как есть", cfg.disk)
        else:
            free = _free_gib(os.path.dirname(cfg.disk))
            lvl = OK if free is None or free >= cfg.disk_size_gb else WARN
            add("drive", lvl, f"Новый qcow2 {cfg.disk_size_gb} ГБ", cfg.disk +
                (f" (свободно {free} ГБ, qcow2 растёт по мере записи)" if free is not None else ""))
    elif cfg.disk_mode == "file":
        if not cfg.disk_path:
            add("drive", ERR, "Не выбран файл диска")
        elif not os.path.exists(cfg.disk_path) and cfg.disk_path not in _pool_paths():
            add("drive", ERR, "Файл диска не найден", cfg.disk_path)
        else:
            add("drive", OK, "Существующий образ", cfg.disk_path)
    else:
        if not cfg.disk_path.startswith("/dev/"):
            add("drive", ERR, "Укажи блочное устройство /dev/…")
        elif not os.path.exists(cfg.disk_path):
            add("drive", ERR, "Устройство не найдено", cfg.disk_path)
        else:
            real = os.path.realpath(cfg.disk_path)
            mounts = open("/proc/mounts").read()
            if re.search(rf"^{re.escape(real)}\d*p?\d*\s", mounts, re.M):
                add("drive", ERR, "Этот диск смонтирован на хосте!", real)
            else:
                add("drive", OK, "Целый диск", f"{cfg.disk_path} → {real}")

    # CPU
    ok, note = model.cpu_option_state(h.cores, cfg.cores, cfg.threads)
    if not ok:
        add("cpu", ERR, f"{cfg.cores}/{cfg.vcpus} нельзя", note)
    else:
        p = model.plan_pinning(h.cores, cfg.cores, cfg.threads)
        add("cpu", WARN if p.all_cores else OK, f"{cfg.cores} ядер / {cfg.vcpus} потоков",
            note or f"хосту остаются CPU {model.cpulist(p.host_cpus)}")
        if p.all_cores and cfg.fifo:
            add("cpu", WARN, "FIFO при всех ядрах отключён", "иначе хост может зависнуть")

    # RAM
    ok, note = model.ram_option_state(cfg.ram_gib, h.mem_mib)
    if not ok:
        add("ram", ERR, f"{cfg.ram_gib} ГБ нельзя", note)
    else:
        add("ram", OK, f"{cfg.ram_gib} ГБ", f"хосту остаётся ~{h.mem_mib // 1024 - cfg.ram_gib} ГиБ")
    if cfg.stealth:
        if smbios.layout_for(cfg.ram_gib) is None:
            add("ram", ERR, "Объём не раскладывается на планки SMBIOS", "выбери кратный 4 ГБ или выключи маскировку")
        else:
            add("ram", OK, "Планки в SMBIOS", model.dimm_text(cfg.ram_gib))

    # Audio
    if cfg.audio == "host":
        if not cfg.audio_pci:
            add("audio", ERR, "Не выбран аудиоконтроллер")
        for a, why in host.group_problems(cfg.audio_pci, h.pci):
            add("audio", ERR, f"{a}: {why}")
        for a in cfg.audio_pci:
            d = h.pci_by_addr.get(a)
            if d:
                add("audio", OK, d.name, f"{d.short}, группа {d.group}")
    else:
        add("audio", OK if any(h.pci_by_addr.get(a, None) and h.pci_by_addr[a].base_class == 0x04 for a in cfg.gpu)
            else WARN, "Звук через HDMI/DP видеокарты", "или USB-звуковуха во вкладке Peripheral")

    # Network
    if cfg.net == "none":
        add("network", WARN, "Сеть выключена")
    elif cfg.net == "nat":
        lvl = OK if cfg.net_source in h.networks else ERR
        add("network", lvl, f"NAT через сеть libvirt «{cfg.net_source}»",
            "" if lvl == OK else "такой сети нет: " + ", ".join(h.networks))
    elif cfg.net == "bridge":
        add("network", OK if cfg.net_source in h.bridges else ERR, f"Мост {cfg.net_source or '—'}",
            "" if cfg.net_source in h.bridges else "мост не найден")
    else:
        add("network", OK if cfg.net_source in h.nics else ERR, f"macvtap на {cfg.net_source or '—'}",
            "хост и VM не увидят друг друга по сети — особенность macvtap")
    if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", cfg.mac) or int(cfg.mac[:2], 16) & 1:
        add("network", ERR, "Неверный MAC", cfg.mac)

    # Peripheral
    if not cfg.usb_controllers and not cfg.usb_devices:
        add("peripheral", ERR, "Нет клавиатуры/мыши для VM",
            "при single-GPU хост без экрана — прокинь USB-контроллер или устройства")
    for a in cfg.usb_controllers:
        for x, why in host.group_problems([a], h.pci):
            add("peripheral", ERR, f"{x}: {why}")
        disks = host.mounted_disks_on(a)
        if disks:
            add("peripheral", ERR, f"На контроллере {a[5:]} смонтирован диск: {', '.join(disks)}",
                "VM заберёт его у хоста — отмонтируй или выбери другой контроллер")
        names = [u.name for u in host.usb_on_controller(a, h.usb)]
        add("peripheral", OK, f"Контроллер {a[5:]}", ", ".join(names) or "сейчас пустой")
    present = {u.ids for u in h.usb}
    for ids in cfg.usb_devices:
        add("peripheral", OK if ids in present else WARN, f"USB {ids}",
            "" if ids in present else "сейчас не подключено (startupPolicy=optional — VM всё равно стартует)")

    # Preset / Save
    if cfg.stealth:
        add("preset", OK, "Маскировка включена", "SMBIOS X99, kvm hidden, без hypervisor-бита")
    else:
        add("preset", OK, "Без маскировки", "Hyper-V enlightenments включены")
    if not model.valid_name(cfg.name):
        add("save", ERR, "Неверное имя VM", "латиница, цифры, - _ .")
    elif cfg.name in h.domains and not is_ours(cfg.name):
        add("save", ERR, f"VM «{cfg.name}» уже есть и создана не этой программой", "выбери другое имя")
    elif cfg.name in h.domains:
        add("save", WARN, f"VM «{cfg.name}» будет перезаписана", "диск и NVRAM сохранятся")
    return out


def is_ours(name):
    rc, xml = host.run(VIRSH + ["dumpxml", name, "--inactive"])
    return rc == 0 and "urn:local:passthru" in xml


_pool_cache = {}


def _pool_list():
    if "default" not in _pool_cache:
        rc, out = host.run(VIRSH + ["vol-list", "default"])
        rows = [l.split(None, 1) for l in out.splitlines()[2:] if l.strip()] if rc == 0 else []
        _pool_cache["default"] = [(r[0], r[1].strip()) for r in rows if len(r) == 2]
    return _pool_cache["default"]


def _pool_volumes():
    return {n for n, _ in _pool_list()}


def _pool_paths():
    return {p for _, p in _pool_list()}


def _free_gib(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize // 1024 ** 3
    except OSError:
        return None


def all_checks(h, cfg):
    return host_checks(h, cfg) + config_checks(h, cfg)


# ---- deep verification (no VM start) -----------------------------------

def verify(h, cfg, workdir=None):
    """Build everything into workdir and check it without starting anything.
    Returns list[Check] with tab 'save'. Without workdir a temp dir is used and removed."""
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="passthru-verify-") as tmp:
            return verify(h, cfg, tmp)
    out = []
    add = lambda lvl, t, d="": out.append(Check("save", lvl, t, d))
    smb_dir = os.path.join(workdir, "smbios")
    os.makedirs(smb_dir, exist_ok=True)

    try:
        xml = xmlgen.build(cfg, h.cores, smbios_dir=smb_dir)
    except Exception as e:  # noqa: BLE001 — report any builder failure as a check
        add(ERR, "Сборка XML", f"{type(e).__name__}: {e}")
        return out
    xml_path = os.path.join(workdir, "domain.xml")
    with open(xml_path, "w") as f:
        f.write(xml)

    if cfg.stealth:
        try:
            files = xmlgen.smbios_files(cfg)
            for n, b in files.items():
                with open(os.path.join(smb_dir, n), "wb") as f:
                    f.write(b)
            total = sum(smbios.dimm_size_mb(b) for n, b in files.items() if n.startswith("t17_"))
            for n, b in files.items():
                if n.endswith(".bin"):
                    smbios.parse(b)
            if total != cfg.ram_gib * 1024:
                add(ERR, "SMBIOS: сумма планок ≠ RAM", f"{total} МБ против {cfg.ram_gib * 1024} МБ")
            else:
                add(OK, "SMBIOS-блобы", f"{len(files)} файлов, планки {model.dimm_text(cfg.ram_gib)} = <memory>")
        except Exception as e:  # noqa: BLE001
            add(ERR, "SMBIOS-блобы", str(e))

    rc, msg = host.run(["virt-xml-validate", xml_path, "domain"])
    add(OK if rc == 0 else ERR, "Схема libvirt (virt-xml-validate)", msg.splitlines()[-1] if msg else "")

    rc, argv = host.run(VIRSH + ["domxml-to-native", "qemu-argv", "--xml", xml_path], timeout=60)
    if rc != 0:
        add(ERR, "libvirt → командная строка QEMU", argv)
    else:
        add(OK, "libvirt → командная строка QEMU", f"{len(argv.split())} аргументов, эмулятор {cfg.emulator}")
        out += argv_checks(cfg, argv)
        with open(os.path.join(workdir, "qemu-argv.txt"), "w") as f:
            f.write(argv + "\n")

    out += hook_checks(h, cfg, workdir)
    return out


def argv_checks(cfg, argv):
    out = []
    add = lambda lvl, t, d="": out.append(Check("save", lvl, t, d))
    if not argv.split("\n")[-1].split(" ", 1)[0].endswith("qemu-system-x86_64") and cfg.emulator not in argv:
        add(ERR, "В командной строке не тот QEMU")
    for a in cfg.gpu + cfg.usb_controllers + (cfg.audio_pci if cfg.audio == "host" else []):
        if f'"host":"{a[5:]}"' not in argv and f"host={a[5:]}" not in argv and f'"host":"{a}"' not in argv:
            add(ERR, f"vfio-pci {a} не попал в командную строку")
    m = re.search(r"-m (?:size=)?(\d+)k?", argv)
    want = cfg.ram_gib * 1024
    got = int(m.group(1)) // (1024 if m and m.group(0).endswith("k") else 1) if m else None
    if got is not None and got != want:
        add(ERR, "Память в командной строке", f"{got} МБ вместо {want}")
    if f"cores={cfg.cores}" not in argv or f"threads={cfg.threads}" not in argv:
        add(ERR, "Топология CPU в командной строке не совпала")
    if cfg.stealth:
        n17 = argv.count("t17_")
        if n17 != 4:
            add(ERR, "В командной строке не 4 блоба type 17", str(n17))
        if re.search(r"-smbios '?type=17,", argv):
            add(ERR, "Есть -smbios type=17 вместе с блобами — QEMU не стартует")
    if not out:
        add(OK, "Командная строка QEMU совпадает с настройками", "vfio, память, топология, SMBIOS")
    return out


def hook_checks(h, cfg, workdir):
    out = []
    add = lambda lvl, t, d="": out.append(Check("save", lvl, t, d))
    try:
        hk = hooks.files(cfg, h.cores, h.dm or "sddm")
    except Exception as e:  # noqa: BLE001
        add(ERR, "Хуки", str(e))
        return out
    hdir = os.path.join(workdir, "hooks")
    state = os.path.join(workdir, "hook-state")
    os.makedirs(state, exist_ok=True)
    for rel in sorted(hk, key=lambda r: (not r.startswith("prepare"), r)):
        p = os.path.join(hdir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(hk[rel])
        rc, msg = host.run(["bash", "-n", p])
        if rc != 0:
            add(ERR, f"Синтаксис {rel}", msg)
            continue
        env = dict(os.environ, PASSTHRU_DRYRUN="1", PASSTHRU_STATE=state)
        try:
            r = subprocess.run(["bash", p, cfg.name, "prepare", "begin", "-"], env=env,
                               capture_output=True, text=True, timeout=30)
            rc, msg = r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, msg = 124, "таймаут"
        cmds = [l for l in msg.splitlines() if l.startswith("+ ")]
        if rc != 0:
            add(ERR, f"Dry-run {rel}", msg[-400:])
        else:
            add(OK, f"Хук {rel}", f"{len(cmds)} действий: " + "; ".join(c[2:] for c in cmds[:4]) +
                (" …" if len(cmds) > 4 else ""))
    if cfg.gpu and not any("display" in r for r in hk):
        add(ERR, "Нет single-GPU хука")
    return out
