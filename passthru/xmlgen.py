"""Config -> libvirt domain XML (+ the SMBIOS files it references)."""

import os
import re
import xml.etree.ElementTree as ET

from . import host, model, smbios

QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
OSINFO_NS = "http://libosinfo.org/xmlns/libvirt/domain/1.0"
PASSTHRU_NS = "urn:local:passthru"
ET.register_namespace("qemu", QEMU_NS)
ET.register_namespace("libosinfo", OSINFO_NS)
ET.register_namespace("passthru", PASSTHRU_NS)

TEMPLATE = os.path.join(os.path.dirname(__file__), "template.xml")
SHARED_SMBIOS = "/var/lib/libvirt/smbios"
# Host-identity blobs copied from the working VM (CPU caches, sensors, ACPI).
SHARED_FILES = ["t07_l1.bin", "t07_l2.bin", "t07_l3.bin", "t26_vcore.bin", "t27_fan.bin", "t28_temp.bin"]
SHARED_ACPI = [("ssdt_thermal.aml", "X99DH"), ("ssdt_pnp.aml", "X99PNP")]
STEALTH_ARGS = [
    ("-machine", "x-oem-id=INTEL,x-oem-table-id=X99DH"),
    ("-smbios", "type=8,internal_reference=,external_reference=USB1,connector_type=18,port_type=16"),
    ("-smbios", "type=8,internal_reference=,external_reference=USB2,connector_type=18,port_type=16"),
    ("-smbios", "type=8,internal_reference=,external_reference=LAN,connector_type=11,port_type=29"),
    ("-smbios", "type=8,internal_reference=,external_reference=Line-Out,connector_type=31,port_type=28"),
    ("-smbios", "type=9,slot_designation=PCIEX16_1,slot_type=179,slot_data_bus_width=13,current_usage=4,"
                "slot_length=4,slot_id=1,slot_characteristics1=4,slot_characteristics2=1"),
    ("-smbios", "type=9,slot_designation=PCIEX16_2,slot_type=179,slot_data_bus_width=13,current_usage=3,"
                "slot_length=4,slot_id=2,slot_characteristics1=4,slot_characteristics2=1"),
    ("-smbios", "type=9,slot_designation=M.2_1,slot_type=179,slot_data_bus_width=13,current_usage=3,"
                "slot_length=3,slot_id=3,slot_characteristics1=4,slot_characteristics2=1"),
]
HYPERV = ["relaxed", "vapic", "spinlocks", "vpindex", "runtime", "synic", "stimer", "reset", "frequencies",
          "tlbflush", "ipi"]
ROOT_PORTS = 15
FIRST_FREE_BUS = 3  # bus 1 = pcie-pci-bridge, bus 2 = emulated USB controller

OVMF_SECURE = "/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd"
OVMF_PLAIN = "/usr/share/edk2/x64/OVMF_CODE.4m.fd"


def _sub(parent, tag, attrib=None, text=None, index=None):
    el = ET.Element(tag, {k: str(v) for k, v in (attrib or {}).items()})
    if text is not None:
        el.text = str(text)
    if index is None:
        parent.append(el)
    else:
        parent.insert(index, el)
    return el


def _pci_addr(addr):
    dom, bus, rest = addr.split(":")
    slot, fn = rest.split(".")
    return {"domain": "0x" + dom, "bus": "0x" + bus, "slot": "0x" + slot, "function": "0x" + fn}


def cpu_smbios_arg():
    name = host.cpu_model()
    m = re.search(r"@\s*([\d.]+)GHz", name)
    base = int(float(m.group(1)) * 1000) if m else 0
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as f:
            top = int(f.read()) // 1000
    except OSError:
        top = base
    maker = "Intel(R) Corporation" if "Intel" in name else ("Advanced Micro Devices, Inc." if "AMD" in name else "")
    arg = f"type=4,manufacturer={maker},version={name.replace(',', ' ')}"
    if top:
        arg += f",max-speed={top}"
    if base:
        arg += f",current-speed={base}"
    return arg


def smbios_files(cfg):
    """{filename: bytes} that go to <bundle>/smbios. Raises ValueError for unbuildable RAM."""
    files = smbios.build(cfg.ram_gib, smbios.default_serials(cfg.uuid))
    for name in SHARED_FILES + [a for a, _ in SHARED_ACPI]:
        p = os.path.join(SHARED_SMBIOS, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                files[name] = f.read()
    return files


def disk_serial(seed):
    import hashlib
    digits = str(int(hashlib.sha256(("disk" + seed).encode()).hexdigest(), 16))[:6]
    return "S3Z9NB0K" + digits


def build(cfg, cores=None, smbios_dir=None):
    """Return the domain XML text for cfg. `cores`: host.cpu_cores() (passed in by tests)."""
    cores = cores if cores is not None else host.cpu_cores()
    smbios_dir = smbios_dir or os.path.join(cfg.bundle_dir, "smbios")
    tree = ET.parse(TEMPLATE)
    dom = tree.getroot()

    dom.find("name").text = cfg.name
    dom.find("uuid").text = cfg.uuid
    osinfo = dom.find(f"metadata/{{{OSINFO_NS}}}libosinfo/{{{OSINFO_NS}}}os")
    osid = model.OS_CHOICES.get(cfg.os, ("", ""))[1]
    if osid:
        osinfo.set("id", osid)
    else:
        dom.find("metadata").remove(dom.find(f"metadata/{{{OSINFO_NS}}}libosinfo"))

    # memory
    dom.find("memory").text = str(cfg.ram_kib)
    dom.find("currentMemory").text = str(cfg.ram_kib)
    mb = dom.find("memoryBacking")
    if not cfg.hugepages:
        mb.remove(mb.find("hugepages"))

    # cpu
    dom.find("vcpu").text = str(cfg.vcpus)
    topo = dom.find("cpu/topology")
    topo.set("cores", str(cfg.cores))
    topo.set("threads", str(cfg.threads))
    if cfg.pin:
        pin = model.plan_pinning(cores, cfg.cores, cfg.threads)
        idx = list(dom).index(dom.find("vcpu")) + 1
        uses_iothread = cfg.disk_bus == "virtio"
        if uses_iothread:
            _sub(dom, "iothreads", text=1, index=idx)
            idx += 1
        tune = _sub(dom, "cputune", index=idx)
        for v, h in enumerate(pin.vcpu_to_host):
            _sub(tune, "vcpupin", {"vcpu": v, "cpuset": h})
        emu = model.cpulist(pin.host_cpus if not pin.all_cores else cores[0].threads)
        _sub(tune, "emulatorpin", {"cpuset": emu})
        if uses_iothread:
            _sub(tune, "iothreadpin", {"iothread": 1, "cpuset": emu})
        if cfg.fifo and not pin.all_cores:
            for v in range(cfg.vcpus):
                _sub(tune, "vcpusched", {"vcpus": v, "scheduler": "fifo", "priority": 1})

    # firmware
    osel = dom.find("os")
    osel.find("type").set("machine", cfg.machine)
    osel.find("nvram").text = f"/var/lib/libvirt/qemu/nvram/{cfg.name}_VARS.fd"
    loader = osel.find("loader")
    if not cfg.secure_boot:
        fw = osel.find("firmware")
        for feat in fw.findall("feature"):
            fw.remove(feat)
        _sub(fw, "feature", {"enabled": "no", "name": "secure-boot"})
        loader.set("secure", "no")
        loader.text = OVMF_PLAIN

    feats = dom.find("features")
    cpu = dom.find("cpu")
    clock = dom.find("clock")
    if not cfg.stealth:
        dom.remove(dom.find("sysinfo"))
        osel.remove(osel.find("smbios"))
        feats.remove(feats.find("kvm"))
        hv = _sub(feats, "hyperv", {"mode": "custom"}, index=2)
        for h in HYPERV:
            attrs = {"state": "on"}
            if h == "spinlocks":
                attrs["retries"] = 8191
            _sub(hv, h, attrs)
        for f in cpu.findall("feature"):
            if f.get("name") == "hypervisor":
                cpu.remove(f)
        for t in clock.findall("timer"):
            if t.get("name") == "hypervclock":
                t.set("present", "yes")
                t.attrib.pop("tickpolicy", None)

    # devices
    devs = dom.find("devices")
    devs.find("emulator").text = cfg.emulator
    bridge = devs.find("controller[@model='pcie-to-pci-bridge']")
    pos = list(devs).index(bridge)
    for i in range(1, ROOT_PORTS + 1):
        slot, fn = (2, i - 1) if i <= 8 else (3, i - 9)
        c = _sub(devs, "controller", {"type": "pci", "index": i, "model": "pcie-root-port"}, index=pos)
        pos += 1
        _sub(c, "model", {"name": "pcie-root-port"})
        _sub(c, "target", {"chassis": i, "port": hex(0x0F + i)})
        a = {"type": "pci", "domain": "0x0000", "bus": "0x00", "slot": f"0x{slot:02x}", "function": f"0x{fn:x}"}
        if fn == 0:
            a["multifunction"] = "on"
        _sub(c, "address", a)

    first = list(devs).index(devs.find("controller"))  # disks go first, like virt-manager
    serial = disk_serial(cfg.uuid)
    disk_type = "block" if cfg.disk_mode == "block" else "file"
    d = _sub(devs, "disk", {"type": disk_type, "device": "disk"}, index=first)
    drv = {"name": "qemu", "type": "raw" if cfg.disk_mode == "block" or cfg.disk.endswith((".raw", ".img")) else "qcow2",
           "cache": "none", "io": "native", "discard": "unmap"}
    if cfg.disk_bus == "virtio" and cfg.pin:
        drv["iothread"] = 1
    _sub(d, "driver", drv)
    _sub(d, "source", {"dev" if disk_type == "block" else "file": cfg.disk})
    tgt = {"dev": "vda" if cfg.disk_bus == "virtio" else "sda", "bus": cfg.disk_bus}
    if cfg.disk_bus == "sata":
        tgt["rotation_rate"] = 1
    _sub(d, "target", tgt)
    _sub(d, "serial", text=serial)
    _sub(d, "boot", {"order": 2})

    letter = iter("bcdef")
    for iso, boot in ((cfg.iso, 1), (cfg.virtio_iso if cfg.os in ("win10", "win11") else "", None)):
        if not iso:
            continue
        first += 1
        c = _sub(devs, "disk", {"type": "file", "device": "cdrom"}, index=first)
        _sub(c, "driver", {"name": "qemu", "type": "raw"})
        _sub(c, "source", {"file": iso})
        _sub(c, "target", {"dev": "sd" + next(letter), "bus": "sata"})
        _sub(c, "readonly")
        if boot:
            _sub(c, "boot", {"order": boot})

    # network
    if cfg.net != "none":
        kind = {"nat": "network", "bridge": "bridge", "direct": "direct"}[cfg.net]
        i = _sub(devs, "interface", {"type": kind})
        _sub(i, "mac", {"address": cfg.mac})
        if kind == "network":
            _sub(i, "source", {"network": cfg.net_source or "default"})
        elif kind == "bridge":
            _sub(i, "source", {"bridge": cfg.net_source})
        else:
            _sub(i, "source", {"dev": cfg.net_source, "mode": "bridge"})
        _sub(i, "model", {"type": cfg.net_model})
        _sub(i, "link", {"state": "up"})

    # TPM
    if cfg.tpm != "none":
        t = _sub(devs, "tpm", {"model": "tpm-crb"}, index=list(devs).index(devs.find("audio")))
        if cfg.tpm == "passthrough":
            b = _sub(t, "backend", {"type": "passthrough"})
            _sub(b, "device", {"path": "/dev/tpm0"})
        else:
            _sub(t, "backend", {"type": "emulator", "version": "2.0"})

    # PCI passthrough: one guest root port per host slot, functions kept together
    pci = list(dict.fromkeys(cfg.gpu + (cfg.audio_pci if cfg.audio == "host" else []) + cfg.usb_controllers))
    slots = {}
    for a in pci:
        slots.setdefault(a.rsplit(".", 1)[0], []).append(a)
    bus = FIRST_FREE_BUS
    wd = devs.find("watchdog")
    for slot, addrs in slots.items():
        addrs.sort()
        for a in addrs:
            h = _sub(devs, "hostdev", {"mode": "subsystem", "type": "pci", "managed": "yes"},
                     index=list(devs).index(wd))
            src = _sub(h, "source")
            _sub(src, "address", _pci_addr(a))
            if a in cfg.gpu and cfg.gpu_rom and a.endswith(".0"):
                _sub(h, "rom", {"file": cfg.gpu_rom})
            fn = int(a.rsplit(".", 1)[1], 16)
            ga = {"type": "pci", "domain": "0x0000", "bus": f"0x{bus:02x}", "slot": "0x00", "function": f"0x{fn:x}"}
            if fn == 0 and len(addrs) > 1:
                ga["multifunction"] = "on"
            _sub(h, "address", ga)
        bus += 1

    for ids in cfg.usb_devices:
        v, p = ids.split(":")
        h = _sub(devs, "hostdev", {"mode": "subsystem", "type": "usb", "managed": "yes"},
                 index=list(devs).index(wd))
        src = _sub(h, "source", {"startupPolicy": "optional"})
        _sub(src, "vendor", {"id": "0x" + v})
        _sub(src, "product", {"id": "0x" + p})

    # QEMU command line
    cmd = dom.find(f"{{{QEMU_NS}}}commandline")
    if cfg.stealth:
        args = list(STEALTH_ARGS[:1])
        files = smbios_files(cfg)
        blob = lambda f: ("-smbios", "file=" + os.path.join(smbios_dir, f))
        args += [blob(f) for f in [f for f in sorted(files) if f.startswith("t06_")] + SHARED_FILES if f in files]
        args += STEALTH_ARGS[1:]
        args += [blob(f) for f in ["t16_array.bin"] + [f for f in sorted(files) if f.startswith("t17_")]]
        args.append(("-smbios", cpu_smbios_arg()))
        for f, table in SHARED_ACPI:
            if f in files:
                args.append(("-acpitable", f"sig=SSDT,rev=2,oem_id=INTEL,oem_table_id={table},"
                                           f"data={os.path.join(smbios_dir, f)}"))
        for a, b in args:
            _sub(cmd, f"{{{QEMU_NS}}}arg", {"value": a})
            _sub(cmd, f"{{{QEMU_NS}}}arg", {"value": b})

    ET.indent(tree, "  ")
    return ET.tostring(dom, encoding="unicode") + "\n"
