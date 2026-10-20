"""VM settings, presets and the rules that turn them into pinning / memory numbers."""

import json
import os
import random
import re
import uuid as uuidlib
from dataclasses import asdict, dataclass, field, fields

from . import host, smbios

CONFIG_DIR = os.path.expanduser("~/.config/passthru")
DATA_DIR = os.path.expanduser("~/.local/share/passthru")
CUSTOM_EMULATOR = "/usr/local/bin/qemu-system-x86_64"
STEALTH_SMBIOS_DIR = "/var/lib/libvirt/smbios"


def default_emulator():
    """Custom stealth QEMU if present, else the distro's qemu-system-x86_64."""
    if os.access(CUSTOM_EMULATOR, os.X_OK):
        return CUSTOM_EMULATOR
    import shutil
    return shutil.which("qemu-system-x86_64") or "/usr/bin/qemu-system-x86_64"


def stealth_available():
    """Masking needs both the custom QEMU and the host-identity SMBIOS blobs.
    Without them this is a plain (portable) passthrough builder."""
    if not os.access(CUSTOM_EMULATOR, os.X_OK):
        return False
    return os.path.exists(os.path.join(STEALTH_SMBIOS_DIR, "t16_array.bin"))


# kept for backwards-compat with saved configs
DEFAULT_EMULATOR = default_emulator()

RAM_PRESETS = [8, 12, 16, 24, 32]
CPU_PRESETS = [(2, 2), (4, 2), (6, 2), (8, 2), (12, 2), (16, 2)]  # (cores, threads per core)
HOST_RAM_RESERVE_MIB = 3 * 1024

OS_CHOICES = {
    "win11": ("Windows 11", "http://microsoft.com/win/11"),
    "win10": ("Windows 10", "http://microsoft.com/win/10"),
    "linux": ("Linux", "http://libosinfo.org/linux/2022"),
    "other": ("Другая", ""),
}

NET_MODELS = {"e1000e": "Intel e1000e", "virtio": "virtio (быстрее, нужен драйвер)", "rtl8139": "Realtek 8139"}
MAC_OUI = {"e1000e": "a4:bb:6d", "virtio": "02:16:3e", "rtl8139": "00:e0:4c"}


@dataclass
class Config:
    name: str = "win11"
    uuid: str = ""
    emulator: str = DEFAULT_EMULATOR
    machine: str = "pc-q35-11.1"

    gpu: list = field(default_factory=list)  # PCI addresses: GPU + its audio etc.
    gpu_rom: str = ""
    unbind_console: bool = False

    os: str = "win11"
    iso: str = ""
    virtio_iso: str = ""
    tpm: str = "passthrough"  # passthrough | emulated | none
    secure_boot: bool = True

    disk_mode: str = "new"  # new | file | block
    disk_path: str = ""
    disk_size_gb: int = 128
    disk_bus: str = "sata"  # sata | virtio

    cores: int = 6
    threads: int = 2
    pin: bool = True
    isolate: bool = True  # move host services off the VM cores while it runs
    fifo: bool = True

    ram_gib: int = 16
    hugepages: bool = True

    audio: str = "none"  # none | host
    audio_pci: list = field(default_factory=list)

    net: str = "nat"  # nat | bridge | direct | none
    net_source: str = "default"
    net_model: str = "e1000e"
    mac: str = ""

    usb_controllers: list = field(default_factory=list)
    usb_devices: list = field(default_factory=list)  # "vvvv:pppp"

    stealth: bool = True
    perf_hooks: bool = True

    def ensure_ids(self):
        if not self.uuid:
            self.uuid = str(uuidlib.uuid4())
        if not self.mac:
            self.mac = random_mac(self.net_model)
        return self

    @property
    def vcpus(self):
        return self.cores * self.threads

    @property
    def ram_kib(self):
        return self.ram_gib * 1024 * 1024

    @property
    def bundle_dir(self):
        return os.path.join(DATA_DIR, "vms", self.name)

    @property
    def default_disk(self):
        return f"/var/lib/libvirt/images/{self.name}.qcow2"

    @property
    def disk(self):
        return self.disk_path or self.default_disk

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def random_mac(model="e1000e"):
    oui = MAC_OUI.get(model, MAC_OUI["e1000e"])
    tail = [random.randint(0, 255) for _ in range(3)]
    return oui + ":" + ":".join(f"{b:02x}" for b in tail)


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")


def valid_name(name):
    return bool(NAME_RE.match(name))


# ---- CPU ---------------------------------------------------------------

@dataclass
class Pinning:
    vcpu_to_host: list  # index = vcpu
    vm_cores: list  # host core indices given to the VM
    host_cpus: list  # host cpu numbers left for the host / emulator
    all_cores: bool  # VM takes every core


def plan_pinning(cores, n, threads):
    """Give the VM `n` physical cores starting from core 1; core 0 stays with the host.
    Raises ValueError when the host can't provide it."""
    if n < 1:
        raise ValueError("нужно хотя бы одно ядро")
    if n > len(cores):
        raise ValueError(f"у процессора только {len(cores)} физ. ядер, а просишь {n}")
    ht = min(len(c.threads) for c in cores)
    if threads > ht:
        raise ValueError(f"у процессора {ht} поток(а) на ядро, а просишь {threads}")
    all_cores = n == len(cores)
    chosen = list(range(len(cores))) if all_cores else list(range(1, n + 1))
    vmap = []
    for ci in chosen:
        vmap += cores[ci].threads[:threads]
    if all_cores:
        host_cpus = [t for c in cores for t in c.threads]
    else:
        host_cpus = [t for i, c in enumerate(cores) if i not in chosen for t in c.threads]
    return Pinning(vmap, chosen, sorted(host_cpus), all_cores)


def cpu_option_state(cores, n, threads):
    """(enabled, note) for a CPU preset button."""
    try:
        p = plan_pinning(cores, n, threads)
    except ValueError as e:
        return False, str(e)
    if p.all_cores:
        return True, "все ядра — хосту ничего не останется, без FIFO"
    return True, ""


def cpulist(cpus):
    """[0,1,2,8] -> '0-2,8'"""
    cpus = sorted(set(cpus))
    out, i = [], 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else f"{cpus[i]}-{cpus[j]}")
        i = j + 1
    return ",".join(out)


# ---- RAM ---------------------------------------------------------------

def ram_limit_gib(total_mib=None):
    total_mib = total_mib if total_mib is not None else host.mem_total_mib()
    return max(0, (total_mib - HOST_RAM_RESERVE_MIB) // 1024)


def ram_option_state(gib, total_mib=None):
    limit = ram_limit_gib(total_mib)
    if gib > limit:
        return False, f"у хоста {(total_mib or host.mem_total_mib()) // 1024} ГиБ, максимум для VM {limit}"
    return True, ""


def dimm_text(gib):
    lay = smbios.layout_for(gib)
    if not lay:
        return "не раскладывается на планки"
    return " + ".join(f"{s}" for s in lay if s) + " ГБ"


# ---- presets -----------------------------------------------------------

BUILTIN_PRESETS = {
    "Игровой Windows 11 (stealth)": dict(os="win11", tpm="passthrough", secure_boot=True, disk_bus="sata",
                                         stealth=True, perf_hooks=True, pin=True, isolate=True, fifo=True,
                                         hugepages=True, net_model="e1000e"),
    "Windows 10 (stealth)": dict(os="win10", tpm="passthrough", secure_boot=True, disk_bus="sata",
                                 stealth=True, perf_hooks=True, pin=True, isolate=True, fifo=True,
                                 hugepages=True, net_model="e1000e"),
    "Windows без маскировки (Hyper-V, virtio)": dict(stealth=False, disk_bus="virtio", net_model="virtio",
                                                    tpm="emulated", perf_hooks=True, pin=True, isolate=True,
                                                    fifo=False, hugepages=True),
    "Linux": dict(os="linux", stealth=False, disk_bus="virtio", net_model="virtio", tpm="none",
                  secure_boot=False, pin=True, isolate=True, fifo=False, hugepages=True),
}


def presets_dir():
    return os.path.join(CONFIG_DIR, "presets")


def user_presets():
    d = presets_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


# Fields that describe this particular VM rather than a reusable recipe.
IDENTITY = ("name", "uuid", "mac", "disk_path")


def save_preset(cfg, title):
    os.makedirs(presets_dir(), exist_ok=True)
    data = {k: v for k, v in asdict(cfg).items() if k not in IDENTITY}
    path = os.path.join(presets_dir(), re.sub(r"[/\0]", "_", title) + ".json")
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def apply_preset(cfg, title):
    if title in BUILTIN_PRESETS:
        data = BUILTIN_PRESETS[title]
    else:
        with open(os.path.join(presets_dir(), title + ".json")) as f:
            data = json.load(f)
    d = asdict(cfg)
    d.update({k: v for k, v in data.items() if k not in IDENTITY and k in d})
    new = Config.from_dict(d)
    if new.net_model != cfg.net_model:
        new.mac = random_mac(new.net_model)
    return new


def last_path():
    return os.path.join(CONFIG_DIR, "last.json")


def load_last():
    try:
        with open(last_path()) as f:
            return Config.from_dict(json.load(f))
    except (OSError, ValueError, TypeError):
        return None


def save_last(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(last_path(), "w") as f:
        f.write(cfg.to_json())
