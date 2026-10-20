#!/bin/sh
# Launcher. /usr/bin/python3 on purpose: it has PyGObject (mise python does not).
cd "$(dirname "$(readlink -f "$0")")" && exec /usr/bin/python3 -m passthru "$@"
