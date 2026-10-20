"""What this machine has: CPU topology, RAM, PCI/IOMMU, USB, ISOs, display manager."""

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

PCI = "/sys/bus/pci/devices"


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def run(cmd, timeout=30, input=None):
    """(returncode, stdout+stderr). Never raises for a missing binary."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: не найден"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: таймаут"


# ---- CPU ---------------------------------------------------------------

@dataclass
class Core:
    id: int
    threads: list  # host cpu numbers, sorted


def cpu_cores():
    """Physical cores of socket 0 ordered by core id, each with its HT siblings."""
    cores = {}
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
        n = int(d.rsplit("cpu", 1)[1])
        if _read(f"{d}/online", "1") == "0":
            continue
        pkg = int(_read(f"{d}/topology/physical_package_id", "0"))
        cid = int(_read(f"{d}/topology/core_id", str(n)))
        cores.setdefault((pkg, cid), []).append(n)
    out = [Core(i, sorted(t)) for i, (_, t) in enumerate(sorted(cores.items(), key=lambda kv: min(kv[1])))]
    return out


def cpu_model():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "?"


def cpu_flags():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("flags"):
            return set(line.split(":", 1)[1].split())
    return set()


def mem_total_mib():
    m = re.search(r"MemTotal:\s+(\d+)", _read("/proc/meminfo"))
    return int(m.group(1)) // 1024 if m else 0


# ---- PCI ---------------------------------------------------------------

@dataclass
class PciDev:
    addr: str  # 0000:06:00.0
    cls: int  # 0x030000
    vendor: str
    device: str
    sub: str  # subsystem vendor:device
    driver: str
    group: str
    name: str = ""
    boot_vga: bool = False

    @property
    def short(self):
        return self.addr[5:]

    @property
    def base_class(self):
        return self.cls >> 16

    @property
    def is_bridge(self):
        return self.cls >> 16 == 0x06

    @property
    def ids(self):
        return f"{self.vendor}:{self.device}"


_names = None


def _lspci_names():
    global _names
    if _names is None:
        _names = {}
        rc, out = run(["lspci", "-D", "-mm"])
        if rc == 0:
            for line in out.splitlines():
                parts = re.findall(r'"([^"]*)"', line)
                addr = line.split()[0]
                if len(parts) >= 3:
                    vendor = parts[1].replace(" Corporation", "").replace(", Inc.", "")
                    _names[addr] = f"{vendor} {parts[2]}"
    return _names


def pci_devices():
    names = _lspci_names()
    devs = []
    for d in sorted(glob.glob(f"{PCI}/*")):
        addr = os.path.basename(d)
        grp = os.path.realpath(f"{d}/iommu_group") if os.path.exists(f"{d}/iommu_group") else ""
        drv = os.path.basename(os.path.realpath(f"{d}/driver")) if os.path.exists(f"{d}/driver") else ""
        devs.append(PciDev(
            addr=addr,
            cls=int(_read(f"{d}/class", "0"), 16),
            vendor=_read(f"{d}/vendor")[2:],
            device=_read(f"{d}/device")[2:],
            sub=_read(f"{d}/subsystem_vendor")[2:] + ":" + _read(f"{d}/subsystem_device")[2:],
            driver=drv,
            group=os.path.basename(grp),
            name=names.get(addr, addr),
            boot_vga=_read(f"{d}/boot_vga") == "1",
        ))
    return devs


def iommu_active():
    return bool(glob.glob("/sys/kernel/iommu_groups/*"))


def group_members(group):
    return sorted(os.listdir(f"/sys/kernel/iommu_groups/{group}/devices")) if group else []


def gpus(devs=None):
    devs = devs or pci_devices()
    return [d for d in devs if d.base_class == 0x03]


def gpu_companions(gpu, devs):
    """Other functions of the same card: same slot, or audio with the same subsystem id
    (Intel Arc puts HDMI audio on a separate bus behind its own switch)."""
    slot = gpu.addr.rsplit(".", 1)[0]
    out = []
    for d in devs:
        if d.addr == gpu.addr or d.is_bridge:
            continue
        same_slot = d.addr.rsplit(".", 1)[0] == slot
        same_card = d.sub == gpu.sub and d.sub != "0000:0000" and d.base_class == 0x04 and d.vendor == gpu.vendor
        if same_slot or same_card:
            out.append(d)
    return out


def group_problems(selected, devs):
    """Devices sharing an IOMMU group with `selected` that are neither selected nor bridges."""
    by_addr = {d.addr: d for d in devs}
    sel = set(selected)
    bad = []
    for a in selected:
        d = by_addr.get(a)
        if not d:
            bad.append((a, "устройство не найдено"))
            continue
        if not d.group:
            bad.append((a, "нет IOMMU-группы (IOMMU выключен?)"))
            continue
        for m in group_members(d.group):
            md = by_addr.get(m)
            if m not in sel and md and not md.is_bridge:
                bad.append((m, f"в той же IOMMU-группе {d.group}, что и {d.short}"))
    return bad


# ---- USB ---------------------------------------------------------------

@dataclass
class UsbDev:
    bus: int
    devnum: int
    vendor: str
    product: str
    name: str
    path: str  # sysfs
    cls: str = ""

    @property
    def ids(self):
        return f"{self.vendor}:{self.product}"


def usb_devices():
    out = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        base = os.path.basename(d)
        if ":" in base or not os.path.exists(f"{d}/idVendor"):
            continue
        vendor, product = _read(f"{d}/idVendor"), _read(f"{d}/idProduct")
        if vendor == "1d6b":  # root hubs
            continue
        name = " ".join(x for x in (_read(f"{d}/manufacturer"), _read(f"{d}/product")) if x) or f"{vendor}:{product}"
        if _read(f"{d}/bDeviceClass") == "09":
            name += " (хаб)"
        out.append(UsbDev(int(_read(f"{d}/busnum", "0")), int(_read(f"{d}/devnum", "0")),
                          vendor, product, name, os.path.realpath(d), _read(f"{d}/bDeviceClass")))
    return out


def usb_on_controller(pci_addr, usb=None):
    usb = usb if usb is not None else usb_devices()
    real = os.path.realpath(f"{PCI}/{pci_addr}")
    return [u for u in usb if u.path.startswith(real + "/")]


def mounted_disks_on(pci_addr):
    """Block devices behind a PCI device that have something mounted."""
    real = os.path.realpath(f"{PCI}/{pci_addr}") + "/"
    mounts = _read("/proc/mounts")
    hits = []
    for b in glob.glob("/sys/block/*"):
        if not os.path.realpath(b).startswith(real):
            continue
        name = os.path.basename(b)
        parts = [name] + [os.path.basename(p) for p in glob.glob(f"{b}/{name}*")]
        holders = []
        for p in parts:
            holders += [os.path.basename(h) for h in glob.glob(f"/sys/class/block/{p}/holders/*")]
        for p in parts + holders:
            if re.search(rf"^/dev/{re.escape(p)}\s", mounts, re.M) or (
                    p.startswith("dm-") and _read(f"/sys/class/block/{p}/dm/name") and
                    re.search(rf"^/dev/mapper/{re.escape(_read(f'/sys/class/block/{p}/dm/name'))}\s", mounts, re.M)):
                hits.append(name)
                break
    return hits


# ---- misc --------------------------------------------------------------

def display_manager():
    link = os.path.realpath("/etc/systemd/system/display-manager.service")
    if link.endswith(".service") and os.path.exists(link):
        return os.path.basename(link)[:-len(".service")]
    return ""


def gpu_driver_family(driver):
    if driver.startswith("nvidia") or driver == "nouveau":
        return "nvidia"
    if driver in ("amdgpu", "radeon"):
        return "amd"
    if driver in ("xe", "i915"):
        return "intel"
    return driver or "нет драйвера"


ISO_DIRS = ["/var/lib/libvirt/images", "~/Downloads", "~/Загрузки", "~/Projects", "~/projects", "~/ISO", "~/iso", "~"]


def find_isos(max_depth=3):
    seen, out = set(), []
    for base in ISO_DIRS:
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            continue
        root_depth = base.rstrip("/").count("/")
        depth_limit = 1 if base == os.path.expanduser("~") else max_depth
        for root, dirs, files in os.walk(base):
            if root.count("/") - root_depth >= depth_limit:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.lower().endswith(".iso"):
                    p = os.path.realpath(os.path.join(root, f))
                    if p not in seen:
                        seen.add(p)
                        out.append(p)
    return out


def guess_os(iso):
    n = os.path.basename(iso).lower()
    if "virtio" in n:
        return "virtio"
    if re.search(r"win(dows)?[ _-]?11", n):
        return "win11"
    if re.search(r"win(dows)?[ _-]?10", n):
        return "win10"
    if any(k in n for k in ("ubuntu", "fedora", "arch", "debian", "mint", "manjaro", "linux", "pop", "cachy", "nixos")):
        return "linux"
    return ""


def libvirt_domains():
    rc, out = run(["virsh", "-c", "qemu:///system", "list", "--all", "--name"])
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def bridges():
    return sorted(os.path.basename(os.path.dirname(b)) for b in glob.glob("/sys/class/net/*/bridge"))


def phys_nics():
    return sorted(os.path.basename(n) for n in glob.glob("/sys/class/net/*") if os.path.exists(f"{n}/device"))


def libvirt_networks():
    rc, out = run(["virsh", "-c", "qemu:///system", "net-list", "--all", "--name"])
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def which(name):
    return shutil.which(name)
