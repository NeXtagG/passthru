"""Headless verification. Never starts a libvirt VM.

    python3 -m passthru --selftest          # every preset combination through libvirt
    python3 -m passthru --selftest --tcg    # + boot a diskless TCG Linux guest (no passthrough,
                                            #   no hooks) on the custom QEMU and read the SMBIOS
                                            #   tables it really produces with dmidecode
"""

import copy
import glob
import itertools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import checks, hooks, host, model, smbios, xmlgen

HERE = os.path.dirname(os.path.abspath(__file__))


def base_config(h):
    gpu = next((g for g in host.gpus(h.pci) if g.boot_vga), None) or host.gpus(h.pci)[0]
    usb = [d.addr for d in h.pci if d.cls >> 8 == 0x0C03 and not host.group_problems([d.addr], h.pci)][:1]
    isos = [i for i in h.isos if host.guess_os(i) != "virtio"]
    return model.Config(name="passthru-selftest", gpu=[gpu.addr] + [c.addr for c in host.gpu_companions(gpu, h.pci)],
                        usb_controllers=usb, iso=isos[0] if isos else "").ensure_ids()


def combos(h):
    oss = list(model.OS_CHOICES)
    rams = [r for r in model.RAM_PRESETS if model.ram_option_state(r, h.mem_mib)[0]]
    cpus = [(c, t) for c, t in model.CPU_PRESETS if model.cpu_option_state(h.cores, c, t)[0]]
    for i, (o, r, (c, t), stealth) in enumerate(itertools.product(oss, rams, cpus, (True, False))):
        # rotate the remaining options so every value is covered without a full product
        yield dict(os=o, ram_gib=r, cores=c, threads=t, stealth=stealth,
                   disk_bus=("sata", "virtio")[i % 2], secure_boot=bool(i % 3),
                   tpm=("passthrough", "emulated", "none")[i % 3] if o != "win11" else ("passthrough", "emulated")[i % 2],
                   hugepages=bool(i % 4), pin=i % 5 != 0, fifo=bool(i % 2), isolate=i % 3 != 1,
                   net=("nat", "none")[i % 7 == 0], net_model=("e1000e", "virtio", "rtl8139")[i % 3],
                   audio=("none", "host")[i % 2], audio_pci=["0000:00:1b.0"] if i % 2 else [],
                   disk_mode=("new", "file")[i % 2], disk_path="" if i % 2 == 0 else "/var/lib/libvirt/images/win11.qcow2")


def run_matrix(h, base, verbose=False):
    fails, n = [], 0
    work = tempfile.mkdtemp(prefix="passthru-matrix-")
    for i, over in enumerate(combos(h)):
        cfg = copy.deepcopy(base)
        for k, v in over.items():
            setattr(cfg, k, v)
        cfg.mac = model.random_mac(cfg.net_model)
        if cfg.audio == "host" and not all(a in h.pci_by_addr for a in cfg.audio_pci):
            cfg.audio, cfg.audio_pci = "none", []
        res = checks.verify(h, cfg, os.path.join(work, str(i)))
        tab = [c for c in checks.config_checks(h, cfg) if c.level == checks.ERR]
        bad = [r for r in res if r.level == checks.ERR] + tab
        n += 1
        tag = f"{cfg.os:5} {cfg.ram_gib:2}G {cfg.cores}/{cfg.vcpus:<2} {'stealth' if cfg.stealth else 'hyperv ':7} " \
              f"{cfg.disk_bus:6} tpm={cfg.tpm:11} sb={int(cfg.secure_boot)} hp={int(cfg.hugepages)} pin={int(cfg.pin)}"
        if bad:
            fails.append((tag, bad))
            print("FAIL", tag)
            for b in bad:
                print("     ", b.title, "—", b.detail[:300])
        elif verbose:
            print("ok  ", tag)
    shutil.rmtree(work, ignore_errors=True)
    return n, fails


def run_disallowed(h, base):
    """Options the host can't do must be reported as errors, never crash."""
    out = []
    for c, t in model.CPU_PRESETS:
        ok, note = model.cpu_option_state(h.cores, c, t)
        cfg = copy.deepcopy(base)
        cfg.cores, cfg.threads = c, t
        errs = [x for x in checks.config_checks(h, cfg) if x.tab == "cpu" and x.level == checks.ERR]
        out.append((f"CPU {c}/{c * t}", ok, bool(errs), note))
    for r in model.RAM_PRESETS:
        ok, note = model.ram_option_state(r, h.mem_mib)
        cfg = copy.deepcopy(base)
        cfg.ram_gib = r
        errs = [x for x in checks.config_checks(h, cfg) if x.tab == "ram" and x.level == checks.ERR]
        out.append((f"RAM {r} GB", ok, bool(errs), note))
    return out


# ---- TCG SMBIOS boot ----------------------------------------------------

def build_initramfs(work):
    init = os.path.join(work, "init")
    rc, msg = host.run(["gcc", "-static", "-Os", "-o", init, os.path.join(HERE, "dmi_init.c")], timeout=120)
    if rc:
        raise RuntimeError("gcc: " + msg)
    os.makedirs(os.path.join(work, "root", "sys"), exist_ok=True)
    os.makedirs(os.path.join(work, "root", "dev"), exist_ok=True)
    shutil.copy(init, os.path.join(work, "root", "init"))
    img = os.path.join(work, "initramfs.cpio")
    files = subprocess.run(["find", ".", "-print"], cwd=os.path.join(work, "root"),
                           capture_output=True, text=True).stdout
    with open(img, "wb") as f:
        subprocess.run(["cpio", "-o", "-H", "newc", "--quiet"], cwd=os.path.join(work, "root"),
                       input=files.encode(), stdout=f, check=True)
    return img


def kernel_image():
    cands = sorted(glob.glob(f"/usr/lib/modules/{os.uname().release}/vmlinuz")) + \
        sorted(glob.glob("/usr/lib/modules/*/vmlinuz"))
    return cands[0] if cands else None


def smbios_args_from_argv(argv):
    toks = shlex.split(argv.splitlines()[-1])
    out = []
    for i, t in enumerate(toks):
        if t in ("-smbios", "-acpitable") and i + 1 < len(toks):
            out += [t, toks[i + 1]]
        if t == "-machine" and i + 1 < len(toks) and "x-oem-id" in toks[i + 1]:
            out += [t, toks[i + 1]]
    return out


def dump_from_serial(text, path):
    def grab(tag):
        a = text.index(f"@@{tag}@@") + len(tag) + 4
        b = text.index("@@end@@", a)
        return bytes.fromhex("".join(text[a:b].split()))
    entry, table = grab("entry"), grab("table")
    entry = bytearray(entry)
    if entry[:5] == b"_SM3_":
        entry[0x10:0x18] = (32).to_bytes(8, "little")
        entry[5] = (256 - (sum(entry[:entry[6]]) - entry[5]) % 256) % 256
    elif entry[:4] == b"_SM_":
        entry[0x18:0x1C] = (32).to_bytes(4, "little")
        entry[0x15] = (256 - (sum(entry[0x10:0x1F]) - entry[0x15]) % 256) % 256
    with open(path, "wb") as f:
        f.write(bytes(entry).ljust(32, b"\0") + table)


def tcg_boot(h, cfg, work, kernel, initrd):
    """Boot the diskless guest with the exact SMBIOS args libvirt would pass. Returns (ok, details)."""
    vdir = os.path.join(work, f"ram{cfg.ram_gib}")
    res = checks.verify(h, cfg, vdir)
    errs = [r for r in res if r.level == checks.ERR]
    if errs:
        return False, "verify: " + errs[0].title
    with open(os.path.join(vdir, "qemu-argv.txt")) as f:
        args = smbios_args_from_argv(f.read())
    cmd = [cfg.emulator, "-machine", f"{cfg.machine},accel=tcg", "-cpu", "max",
           "-smp", f"{cfg.vcpus},sockets=1,cores={cfg.cores},threads={cfg.threads}", "-m", f"{cfg.ram_gib}G",
           "-nodefaults", "-display", "none", "-serial", "stdio", "-no-reboot",
           "-kernel", kernel, "-initrd", initrd, "-append", "console=ttyS0 panic=-1 quiet"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return False, "гость не выключился за 240 с"
    out = p.stdout + p.stderr
    if "@@table@@" not in out:
        return False, f"QEMU rc={p.returncode}: " + out[-600:]
    dump = os.path.join(vdir, "dmi.bin")
    try:
        dump_from_serial(out, dump)
    except ValueError:
        return False, "дамп с последовательного порта обрезан"
    rc, dmi = host.run(["dmidecode", "--from-dump", dump])
    if rc:
        return False, "dmidecode: " + dmi[-300:]
    sizes = []
    for block in dmi.split("\n\n"):
        if "Memory Device" in block and "DMI type 17" in block:
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("Size:"):
                    sizes.append(line[5:].strip())
    total = 0
    for s in sizes:
        num, _, unit = s.partition(" ")
        if num.isdigit():
            total += int(num) * (1024 if unit in ("GB", "GiB") else 1)
    lay = smbios.layout_for(cfg.ram_gib)
    want = [f"{x} GiB" if x else "No Module Installed" for x in lay]
    checks_ = {
        "planks": sizes == want,
        "total": total == cfg.ram_gib * 1024,
        "board": "X99-A II" in dmi,
        "array_slots": "Number Of Devices: 4" in dmi,
        "cpu": host.cpu_model().split("@")[0].strip() in dmi,
    }
    with open(os.path.join(vdir, "dmidecode.txt"), "w") as f:
        f.write(dmi)
    ok = all(checks_.values())
    return ok, f"планки {sizes}, сумма {total} МБ, " + ", ".join(f"{k}={'ok' if v else 'FAIL'}"
                                                                for k, v in checks_.items())


def main(argv):
    h = checks.HostInfo()
    base = base_config(h)
    print(f"Хост: {h.cpu_name}, {len(h.cores)} ядер, {h.mem_mib // 1024} ГиБ, DM={h.dm}")
    print(f"База: GPU {base.gpu}, USB {base.usb_controllers}, ISO {base.iso}")
    fails = 0

    print("\n== Хост")
    for c in checks.host_checks(h, base):
        print(f"  {c.level:5} {c.title} — {c.detail}")
        fails += c.level == checks.ERR

    print("\n== Недоступные варианты должны быть ошибкой, а не падением")
    for name, ok, flagged, note in run_disallowed(h, base):
        good = ok != flagged
        fails += not good
        print(f"  {'ok  ' if good else 'FAIL'} {name:12} {'доступно' if ok else 'недоступно: ' + note}")

    print("\n== Матрица (схема libvirt + domxml-to-native + SMBIOS + dry-run хуков)")
    n, bad = run_matrix(h, base, verbose="-v" in argv)
    fails += len(bad)
    print(f"  {n - len(bad)}/{n} комбинаций прошли")

    print("\n== Хуки отказываются портить экран при нехватке hugepages")
    fails += not hook_abort_test(h, base)

    if "--tcg" in argv:
        print("\n== TCG-гость: реальные таблицы SMBIOS из своего QEMU (без проброса и хуков)")
        work = tempfile.mkdtemp(prefix="passthru-tcg-")
        kernel = kernel_image()
        if not kernel:
            print("  FAIL нет /usr/lib/modules/*/vmlinuz")
            fails += 1
        else:
            initrd = build_initramfs(work)
            for r in [x for x in model.RAM_PRESETS if smbios.layout_for(x)]:
                cfg = copy.deepcopy(base)
                cfg.ram_gib, cfg.stealth = r, True
                cfg.cores, cfg.threads = 4, 2
                ok, msg = tcg_boot(h, cfg, work, kernel, initrd)
                fails += not ok
                print(f"  {'ok  ' if ok else 'FAIL'} {r:2} GB: {msg}")
        print(f"  (файлы: {work})")

    print("\nИТОГ:", "всё прошло" if not fails else f"провалов: {fails}")
    return 1 if fails else 0


def hook_abort_test(h, base):
    """Simulate the display hook after a failed perf hook: it must exit 1 and not stop the DM."""
    cfg = copy.deepcopy(base)
    hk = hooks.files(cfg, h.cores, "sddm")
    work = tempfile.mkdtemp(prefix="passthru-hook-")
    state = os.path.join(work, "state")
    os.makedirs(state)
    open(os.path.join(state, "abort"), "w").close()
    p = os.path.join(work, "display.sh")
    with open(p, "w") as f:
        f.write(hk["prepare/begin/90-passthru-display.sh"])
    r = subprocess.run(["bash", p], env=dict(os.environ, PASSTHRU_DRYRUN="1", PASSTHRU_STATE=state),
                       capture_output=True, text=True)
    ok = r.returncode == 1 and "systemctl stop" not in r.stdout
    print(f"  {'ok  ' if ok else 'FAIL'} abort-метка → exit {r.returncode}, DM не трогается: {'systemctl stop' not in r.stdout}")
    shutil.rmtree(work, ignore_errors=True)
    return ok
