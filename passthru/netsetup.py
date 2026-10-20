"""Make the VM actually reach the internet: IP forwarding + firewall rules.

libvirt installs its own NAT/forward rules, but on hosts with ufw or firewalld
the forwarded traffic from virbrN is dropped anyway, so a NAT VM comes up with
no internet. This module detects the active firewall and emits idempotent root
commands to fix it. Everything needs root, so it is run through sudo in a
terminal, exactly like the hooks. Nothing here starts a VM.
"""

import os
import re

from . import host

VIRSH = ["virsh", "-c", "qemu:///system"]
SYSCTL_FILE = "/etc/sysctl.d/99-passthru.conf"


def active_firewall():
    """Which firewall is actually running: 'ufw' | 'firewalld' | 'nftables' | 'iptables' | 'none'."""
    for svc, name in (("firewalld", "firewalld"), ("ufw", "ufw"), ("nftables", "nftables")):
        if host.run(["systemctl", "is-active", "--quiet", svc])[0] == 0:
            return name
    # iptables with rules but no service (rare): only count it if the filter table has a drop policy
    if host.which("iptables"):
        rc, out = host.run(["iptables", "-S", "FORWARD"])
        if rc == 0 and "-P FORWARD DROP" in out:
            return "iptables"
    return "none"


def ip_forward_on():
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def net_bridge(network):
    """Bridge device of a libvirt network (virbr0 for 'default'), or '' if unknown."""
    rc, out = host.run(VIRSH + ["net-dumpxml", network])
    if rc != 0:
        return ""
    m = re.search(r"<bridge name='([^']+)'", out)
    return m.group(1) if m else ""


def net_active(network):
    rc, out = host.run(VIRSH + ["net-list", "--name"])
    return rc == 0 and network in out.split()


def bridge_for(cfg):
    """The host bridge VM traffic goes through, for firewall rules."""
    if cfg.net == "nat":
        return net_bridge(cfg.net_source) or "virbr0"
    if cfg.net == "bridge":
        return cfg.net_source
    return ""  # direct/macvtap and none: no host bridge to open


def status(cfg):
    """(level, title, detail) for the Network tab. Best effort without root."""
    from .checks import OK, WARN
    if cfg.net == "none":
        return OK, "Сеть выключена", ""
    fw = active_firewall()
    if cfg.net == "nat" and not net_active(cfg.net_source):
        return WARN, f"Сеть libvirt «{cfg.net_source}» не запущена", "кнопка ниже поднимет её и настроит firewall"
    if cfg.net == "nat" and not ip_forward_on():
        return WARN, "IP-форвардинг выключен", "без него NAT не работает — нажми «Настроить сеть»"
    if fw == "none":
        return OK, "Firewall не активен", "libvirt сам настроит NAT"
    label = {"ufw": "ufw", "firewalld": "firewalld", "nftables": "nftables", "iptables": "iptables"}[fw]
    if cfg.net == "direct":
        return WARN, f"macvtap при активном {label}",\
            "хост и VM не видят друг друга (особенность macvtap); интернет в VM есть"
    return WARN, f"Активен {label}", "если в VM не будет интернета — нажми «Настроить сеть и firewall»"


def _sysctl_block():
    return [
        "# --- IP forwarding (нужно для NAT) ---",
        f'install -Dm644 /dev/stdin {SYSCTL_FILE} <<<"net.ipv4.ip_forward=1"',
        "sysctl -q -w net.ipv4.ip_forward=1",
        # ufw keeps its own copy that wins on reload
        "if [ -f /etc/ufw/sysctl.conf ]; then",
        r"    sed -i 's|^#*net/ipv4/ip_forward=.*|net/ipv4/ip_forward=1|' /etc/ufw/sysctl.conf",
        "fi",
    ]


def _ufw_block(bridge):
    return [
        f'# --- ufw: разрешить форвардинг через мост {bridge} ---',
        f"ufw route allow in on {bridge}",
        f"ufw route allow out on {bridge}",
        "ufw reload",
    ]


def _firewalld_block(bridge):
    return [
        f'# --- firewalld: мост {bridge} в доверенную зону ---',
        f"firewall-cmd --permanent --zone=trusted --change-interface={bridge} || true",
        f"firewall-cmd --zone=trusted --change-interface={bridge} || true",
        "firewall-cmd --reload",
    ]


def _nftables_block(bridge):
    return [
        f'# --- nftables: свой ruleset может резать форвардинг моста {bridge} ---',
        'echo "ВНИМАНИЕ: активен свой nftables. libvirt ставит свои правила в таблицу \\"libvirt\\","',
        'echo "но твой forward-chain может их перекрывать. Если в VM нет интернета, добавь в него:"',
        f'echo "    iifname \\"{bridge}\\" accept; oifname \\"{bridge}\\" accept"',
    ]


def _restart_net(network):
    return [
        f'# --- перечитать правила сети libvirt «{network}» ---',
        f"virsh -c qemu:///system net-autostart {network} || true",
        f"if virsh -c qemu:///system net-list --name | grep -qx {network}; then",
        f'    echo "сеть {network} уже запущена — правила переустановит только перезапуск самой сети"',
        "else",
        f"    virsh -c qemu:///system net-start {network} || true",
        "fi",
    ]


def plan(cfg):
    """List of shell command lines (run as root) to give the VM working networking.
    Empty list means nothing to do (no network, or no firewall and forwarding on)."""
    if cfg.net in ("none", "direct"):
        return []
    fw = active_firewall()
    bridge = bridge_for(cfg)
    lines = []
    if cfg.net == "nat":
        lines += _sysctl_block()
    if bridge and fw != "none":
        block = {"ufw": _ufw_block, "firewalld": _firewalld_block,
                 "nftables": _nftables_block, "iptables": lambda _b: []}[fw](bridge)
        lines += block
    if cfg.net == "nat":
        lines += _restart_net(cfg.net_source)
    return lines


def script(cfg):
    lines = ["#!/bin/bash",
             f"# passthru: сеть и firewall для VM «{cfg.name}». Безопасно запускать повторно.",
             'set -e',
             '[ "$(id -u)" = 0 ] || { echo "Нужен root: sudo bash $0"; exit 1; }',
             'set -x']
    body = plan(cfg)
    if not body:
        lines.append('set +x; echo "Для этой конфигурации ничего настраивать не нужно."')
    else:
        lines += body
        lines.append('set +x; echo; echo "Готово. Сеть и firewall настроены."')
    return "\n".join(lines) + "\n"


def script_path(cfg):
    return os.path.join(cfg.bundle_dir, "setup-network.sh")


def needed(cfg):
    return bool(plan(cfg))
