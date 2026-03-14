# Gentoo Auto Updater — PyQt6 Edition

System-tray app for automatic Gentoo Linux updates with KDE Plasma integration.

## Installation

```bash
# 1. Install PyQt6 on Gentoo
sudo emerge -av dev-python/pyqt6 app-portage/gentoolkit x11-libs/libnotify

# 2. Deploy
sudo mkdir -p /opt/gentoo-updater
sudo cp gentoo_updater.py /opt/gentoo-updater/

# 3. KDE launcher + autostart
cp gentoo-updater.desktop ~/.local/share/applications/
cp gentoo-updater.desktop ~/.config/autostart/

# 4. Sudoers — edit YOUR_USERNAME first
sudo visudo -f /etc/sudoers.d/gentoo-updater
# YOUR_USERNAME ALL=(root) NOPASSWD: /usr/bin/emerge, /usr/bin/eselect, \
#   /usr/bin/revdep-rebuild, /usr/bin/eclean-dist, /usr/bin/eclean-pkg

# 5. Run
python3 /opt/gentoo-updater/gentoo_updater.py &
```

---

## Features

- **System tray** — double-click to open, right-click for quick actions
- **Auto-check** — configurable interval (default 6 h)
- **4-step full update** — sync → @world → preserved-rebuild → depclean
- **Live log** — colour-coded emerge output streamed in real time
- **Packages tab** — lists every pending package after a check
- **KDE notifications** — via `notify-send`
- **Abort** — stops any running task cleanly
- **Dark theme** — VS Code–inspired palette
- **Persistent config** — `~/.local/share/gentoo-updater/config.json`
