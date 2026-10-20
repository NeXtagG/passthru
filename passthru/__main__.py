import sys

if "--selftest" in sys.argv:
    from . import selftest
    sys.exit(selftest.main(sys.argv[1:]))

if "--print-netsetup" in sys.argv:
    # Generic NAT + firewall script for the installer to run as root. No GUI import.
    from . import model, netsetup
    cfg = model.Config(name="passthru", net="nat", net_source="default")
    sys.stdout.write(netsetup.script(cfg))
    sys.exit(0)

from . import ui

sys.exit(ui.main(sys.argv[:1]))
