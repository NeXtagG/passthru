"""Write a VM bundle, create its disk, define it in libvirt. Never starts the VM.

Bundle: ~/.local/share/passthru/vms/<name>/
    domain.xml  config.json  smbios/*.bin  hooks/...  install-hooks.sh  uninstall-hooks.sh
Hooks need root, so they are installed by install-hooks.sh run through sudo.
"""

import os
import shlex
import shutil
import subprocess

from . import checks, hooks, host, model, netsetup, xmlgen

VIRSH = checks.VIRSH


def _write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.chmod(path, mode)


def write_bundle(h, cfg):
    d = cfg.bundle_dir
    os.makedirs(d, mode=0o755, exist_ok=True)
    for p in (model.DATA_DIR, os.path.join(model.DATA_DIR, "vms"), d):
        os.chmod(p, 0o755)  # libvirt-qemu must traverse to the SMBIOS blobs
    smb = os.path.join(d, "smbios")
    if os.path.isdir(smb):
        shutil.rmtree(smb)
    if cfg.stealth:
        for n, b in xmlgen.smbios_files(cfg).items():
            _write(os.path.join(smb, n), b)
    xml = xmlgen.build(cfg, h.cores, smbios_dir=smb)
    _write(os.path.join(d, "domain.xml"), xml)
    _write(os.path.join(d, "config.json"), cfg.to_json())

    hk = hooks.files(cfg, h.cores, h.dm or "sddm")
    hdir = os.path.join(d, "hooks")
    if os.path.isdir(hdir):
        shutil.rmtree(hdir)
    for rel, text in hk.items():
        _write(os.path.join(hdir, rel), text, 0o755)
    _write(os.path.join(hdir, "dispatcher"), hooks.DISPATCHER_SCRIPT, 0o755)
    _write(os.path.join(d, "install-hooks.sh"), hooks.install_script(cfg, sorted(hk)), 0o755)
    _write(os.path.join(d, "uninstall-hooks.sh"), hooks.uninstall_script(cfg), 0o755)
    _write(os.path.join(d, "setup-network.sh"), netsetup.script(cfg), 0o755)
    return d, hk


def create_disk(cfg):
    """Create a new qcow2 in the 'default' pool if needed. Returns (ok, message)."""
    if cfg.disk_mode != "new":
        return True, "используется существующий диск"
    if os.path.exists(cfg.disk) or os.path.basename(cfg.disk) in checks._pool_volumes():
        return True, f"{cfg.disk} уже есть — оставлен как есть"
    pool_dir = "/var/lib/libvirt/images"
    if os.path.dirname(cfg.disk) != pool_dir:
        rc, out = host.run(["qemu-img", "create", "-f", "qcow2", cfg.disk, f"{cfg.disk_size_gb}G"])
    else:
        rc, out = host.run(VIRSH + ["vol-create-as", "default", os.path.basename(cfg.disk),
                                    f"{cfg.disk_size_gb}G", "--format", "qcow2"], timeout=120)
    checks._pool_cache.clear()
    return rc == 0, out


def define(cfg):
    rc, out = host.run(VIRSH + ["define", os.path.join(cfg.bundle_dir, "domain.xml")])
    return rc == 0, out


def install_command(cfg):
    return f"sudo bash {shlex.quote(os.path.join(cfg.bundle_dir, 'install-hooks.sh'))}"


def open_terminal(cmd):
    """Run cmd in a new terminal window and keep it open until Enter."""
    script = f"{cmd}; echo; read -rp 'Нажми Enter, чтобы закрыть…'"
    for term in (["xdg-terminal-exec", "bash", "-c", script],
                 ["alacritty", "-e", "bash", "-c", script],
                 ["kitty", "bash", "-c", script],
                 ["foot", "bash", "-c", script]):
        if host.which(term[0]):
            subprocess.Popen(term, start_new_session=True)
            return True
    return False


def hooks_state(h, cfg):
    try:
        return hooks.installed_state(cfg, hooks.files(cfg, h.cores, h.dm or "sddm"))
    except ValueError:
        return "unknown"
