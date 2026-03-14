#!/usr/bin/env python3
"""
Gentoo Auto Updater — KDE Plasma Application (PyQt6 edition)
A system-tray application for automatic Gentoo Linux updates with KDE integration.

Dependencies (Gentoo):
    sudo emerge -av dev-python/PyQt6 app-portage/gentoolkit x11-libs/libnotify
"""

import sys
import subprocess
import logging
import json
from datetime import datetime
from pathlib import Path

# ── PyQt6 imports ─────────────────────────────────────────────────────────────
# PyQt6 key changes vs PyQt5:
#   • All enums are fully-qualified  (Qt.AlignmentFlag.AlignLeft, not Qt.AlignLeft)
#   • QAction / QActionGroup moved to QtGui
#   • app.exec_() → app.exec()
#   • QTextCursor.End → QTextCursor.MoveOperation.End
#   • QSystemTrayIcon.DoubleClick → QSystemTrayIcon.ActivationReason.DoubleClick
#   • QSystemTrayIcon.Information  → QSystemTrayIcon.MessageIcon.Information
# ─────────────────────────────────────────────────────────────────────────────
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMainWindow,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QProgressBar, QCheckBox, QSpinBox, QGroupBox,
    QTabWidget, QListWidget, QListWidgetItem, QMessageBox,
    QFormLayout, QFrame,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QFont, QTextCursor, QAction   # QAction is in QtGui in Qt6


# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / ".local" / "share" / "gentoo-updater"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "updater.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

CONFIG_FILE = LOG_DIR / "config.json"


# ── Default configuration ─────────────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    "check_interval_hours": 6,
    "auto_sync":            True,
    "auto_update":          False,
    "update_world":         True,
    "deep_clean":           True,
    "preserved_rebuild":    True,
    "notify_on_updates":    True,
    "notify_on_complete":   True,
    "emerge_opts":          "--ask=n --verbose --tree",
    "exclude_packages":     [],
    "use_flags_check":      True,
    "news_check":           True,
    "last_check":           None,
    "last_update":          None,
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as exc:
            logger.warning("Could not load config: %s", exc)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as exc:
        logger.error("Could not save config: %s", exc)


# ── Worker thread ─────────────────────────────────────────────────────────────
class UpdateWorker(QThread):
    """Runs emerge commands in a background thread and streams output."""

    output_line    = pyqtSignal(str, str)   # (text, level)
    progress       = pyqtSignal(int)        # 0-100
    finished       = pyqtSignal(bool, str)  # (success, summary)
    packages_found = pyqtSignal(list)       # list[str]

    def __init__(self, task: str, config: dict) -> None:
        super().__init__()
        self.task   = task   # "check" | "sync" | "update" | "clean" | "news"
        self.config = config
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    # ── internal helpers ──────────────────────────────────────────────────────
    def _run(self, cmd: list[str], sudo: bool = True) -> tuple[bool, str]:
        if sudo:
            cmd = ["sudo"] + cmd
        self.output_line.emit(f"$ {' '.join(cmd)}", "cmd")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            collected: list[str] = []
            for raw in proc.stdout:
                if self._abort:
                    proc.terminate()
                    return False, "Aborted by user"
                line = raw.rstrip()
                collected.append(line)
                level = (
                    "error" if "error"   in line.lower() else
                    "warn"  if "warning" in line.lower() else
                    "info"
                )
                self.output_line.emit(line, level)
            proc.wait()
            return proc.returncode == 0, "\n".join(collected)
        except Exception as exc:
            msg = str(exc)
            self.output_line.emit(f"Exception: {msg}", "error")
            return False, msg

    @staticmethod
    def _parse_packages(text: str) -> list[str]:
        pkgs: list[str] = []
        for line in text.splitlines():
            if line.startswith("[ebuild") or line.startswith("[binary"):
                parts = line.split()
                if len(parts) >= 3:
                    pkgs.append(parts[2])
        return pkgs

    # ── task dispatch ─────────────────────────────────────────────────────────
    def run(self) -> None:
        try:
            dispatch = {
                "check":  self._do_check,
                "sync":   self._do_sync,
                "update": self._do_update,
                "clean":  self._do_clean,
                "news":   self._do_news,
            }
            dispatch.get(self.task, lambda: None)()
        except Exception as exc:
            self.finished.emit(False, str(exc))

    def _do_check(self) -> None:
        self.output_line.emit("Checking for available updates…", "info")
        self.progress.emit(10)
        ok, out = self._run(
            ["emerge", "--pretend", "--update", "--deep", "--newuse", "@world"]
        )
        self.progress.emit(90)
        if ok:
            pkgs = self._parse_packages(out)
            self.packages_found.emit(pkgs)
            summary = (
                f"Found {len(pkgs)} package(s) to update."
                if pkgs else "System is up to date."
            )
            self.finished.emit(True, summary)
        else:
            self.finished.emit(False, "Check failed — see log for details.")
        self.progress.emit(100)

    def _do_sync(self) -> None:
        self.output_line.emit("Syncing Portage tree…", "info")
        self.progress.emit(5)
        ok, _ = self._run(["emerge", "--sync"])
        self.progress.emit(50)
        if ok:
            if self.config.get("news_check"):
                self._do_news()
            self.progress.emit(90)
            self.finished.emit(True, "Portage tree synced successfully.")
        else:
            self.finished.emit(False, "Sync failed.")
        self.progress.emit(100)

    def _do_update(self) -> None:
        self.output_line.emit("Starting system update…", "info")
        steps, done = 4, 0

        def step(n: int) -> None:
            nonlocal done
            done += n
            self.progress.emit(int(done / steps * 95))

        # 1 — sync
        if self.config.get("auto_sync"):
            self.output_line.emit("Step 1/4 — Syncing Portage tree", "info")
            ok, _ = self._run(["emerge", "--sync"])
            if not ok:
                self.finished.emit(False, "Sync failed before update.")
                return
        step(1)

        # 2 — @world
        opts = self.config.get("emerge_opts", "--ask=n --verbose").split()
        cmd  = ["emerge"] + opts + ["--update", "--deep", "--newuse", "@world"]
        self.output_line.emit("Step 2/4 — Updating @world", "info")
        ok, _ = self._run(cmd)
        if not ok:
            self.finished.emit(False, "emerge @world failed.")
            return
        step(1)

        # 3 — preserved-rebuild
        if self.config.get("preserved_rebuild"):
            self.output_line.emit("Step 3/4 — Rebuilding preserved packages", "info")
            self._run(["emerge", "@preserved-rebuild"])
        step(1)

        # 4 — depclean + revdep-rebuild
        if self.config.get("deep_clean"):
            self.output_line.emit("Step 4/4 — Cleaning obsolete packages", "info")
            self._run(["emerge", "--depclean"])
            self._run(["revdep-rebuild"])
        step(1)

        self.progress.emit(100)
        self.finished.emit(True, "System update completed successfully!")

    def _do_clean(self) -> None:
        self.output_line.emit("Running system cleanup…", "info")
        self.progress.emit(20)
        self._run(["emerge", "--depclean"])
        self.progress.emit(60)
        self._run(["revdep-rebuild"])
        self.progress.emit(90)
        self._run(["eclean-dist", "--deep"])
        self.progress.emit(100)
        self.finished.emit(True, "Cleanup complete.")

    def _do_news(self) -> None:
        self.output_line.emit("Checking Gentoo news…", "info")
        ok, out = self._run(["eselect", "news", "list"])
        if ok:
            self.output_line.emit(out, "info")


# ── Main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, config: dict) -> None:
        super().__init__()
        self.config         = config
        self.worker: UpdateWorker | None = None
        self._pending_task: str | None   = None
        self.pkg_list: list[str]         = []

        self.setWindowTitle("Gentoo Auto Updater")
        self.setMinimumSize(880, 640)
        self._build_ui()
        self._apply_dark_theme()

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._run_task("check"))
        self._reset_timer()

        QTimer.singleShot(3000, lambda: self._run_task("check"))

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # header
        header = QFrame()
        header.setFixedHeight(64)
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        title_lbl = QLabel("🐉  Gentoo Auto Updater")
        title_lbl.setObjectName("title")
        hl.addWidget(title_lbl)
        hl.addStretch()
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setObjectName("statusBadge")
        hl.addWidget(self.status_lbl)
        root.addWidget(header)

        # tabs
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        root.addWidget(tabs, 1)
        tabs.addTab(self._build_dashboard_tab(), "Dashboard")
        tabs.addTab(self._build_log_tab(),       "Output Log")
        tabs.addTab(self._build_packages_tab(),  "Packages")
        tabs.addTab(self._build_settings_tab(),  "Settings")

        # bottom toolbar
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(12, 6, 12, 6)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        tl.addWidget(self.progress_bar, 1)
        self.btn_abort = QPushButton("Abort")
        self.btn_abort.setObjectName("btnDanger")
        self.btn_abort.setEnabled(False)
        self.btn_abort.clicked.connect(self._abort_task)
        tl.addWidget(self.btn_abort)
        root.addWidget(toolbar)

    def _build_dashboard_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # stat cards
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_status   = self._stat_card("System Status",   "Unknown", "#4ec9b0")
        self.card_packages = self._stat_card("Pending Updates",  "—",      "#ce9178")
        self.card_last     = self._stat_card("Last Check",       "Never",  "#9cdcfe")
        self.card_updated  = self._stat_card("Last Update",      "Never",  "#dcdcaa")
        for c in (self.card_status, self.card_packages, self.card_last, self.card_updated):
            cards.addWidget(c)
        layout.addLayout(cards)

        # action buttons
        grp = QGroupBox("Actions")
        grp.setObjectName("actionGroup")
        btn_row = QHBoxLayout(grp)
        btn_row.setSpacing(10)
        for label, task, obj in (
            ("🔍  Check Updates",  "check",  "btnPrimary"),
            ("🔄  Sync Portage",   "sync",   "btnSecondary"),
            ("⬆️  Update System",  "update", "btnSuccess"),
            ("🧹  Clean System",   "clean",  "btnWarning"),
            ("📰  Read News",      "news",   "btnInfo"),
        ):
            btn = QPushButton(label)
            btn.setObjectName(obj)
            btn.setMinimumHeight(42)
            btn.clicked.connect(lambda _, t=task: self._run_task(t))
            btn_row.addWidget(btn)
        layout.addWidget(grp)
        layout.addStretch()

        auto_row = QHBoxLayout()
        self.chk_auto = QCheckBox("Enable automatic updates")
        self.chk_auto.setChecked(self.config.get("auto_update", False))
        self.chk_auto.toggled.connect(self._toggle_auto_update)
        auto_row.addWidget(self.chk_auto)
        auto_row.addStretch()
        layout.addLayout(auto_row)
        return w

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 8, 12, 8)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Monospace", 9))
        self.log_view.setObjectName("logView")
        layout.addWidget(self.log_view)
        btn_clear = QPushButton("Clear Log")
        btn_clear.setObjectName("btnSecondary")
        btn_clear.clicked.connect(self.log_view.clear)
        # Qt6: use fully-qualified enum
        layout.addWidget(btn_clear, 0, Qt.AlignmentFlag.AlignRight)
        return w

    def _build_packages_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(QLabel("Packages pending update:"))
        self.pkg_list_widget = QListWidget()
        self.pkg_list_widget.setObjectName("pkgList")
        layout.addWidget(self.pkg_list_widget)
        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(20, 16, 20, 16)

        form_grp = QGroupBox("Update Settings")
        form = QFormLayout(form_grp)
        form.setSpacing(10)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 168)
        self.spin_interval.setValue(self.config.get("check_interval_hours", 6))
        self.spin_interval.setSuffix(" hours")
        form.addRow("Check interval:", self.spin_interval)

        self.chk_sync     = QCheckBox("Auto-sync before update")
        self.chk_sync.setChecked(self.config.get("auto_sync", True))
        form.addRow(self.chk_sync)

        self.chk_world    = QCheckBox("Update @world")
        self.chk_world.setChecked(self.config.get("update_world", True))
        form.addRow(self.chk_world)

        self.chk_prebuilt = QCheckBox("Rebuild preserved packages")
        self.chk_prebuilt.setChecked(self.config.get("preserved_rebuild", True))
        form.addRow(self.chk_prebuilt)

        self.chk_deep     = QCheckBox("Deep clean after update")
        self.chk_deep.setChecked(self.config.get("deep_clean", True))
        form.addRow(self.chk_deep)

        self.chk_news     = QCheckBox("Check news after sync")
        self.chk_news.setChecked(self.config.get("news_check", True))
        form.addRow(self.chk_news)

        self.emerge_opts_edit = QTextEdit()
        self.emerge_opts_edit.setPlainText(
            self.config.get("emerge_opts", "--ask=n --verbose --tree")
        )
        self.emerge_opts_edit.setFixedHeight(48)
        form.addRow("emerge options:", self.emerge_opts_edit)
        layout.addWidget(form_grp)

        notify_grp = QGroupBox("Notifications")
        nf = QFormLayout(notify_grp)
        self.chk_notify_found    = QCheckBox("Notify when updates are found")
        self.chk_notify_found.setChecked(self.config.get("notify_on_updates", True))
        self.chk_notify_complete = QCheckBox("Notify when update completes")
        self.chk_notify_complete.setChecked(self.config.get("notify_on_complete", True))
        nf.addRow(self.chk_notify_found)
        nf.addRow(self.chk_notify_complete)
        layout.addWidget(notify_grp)

        save_btn = QPushButton("💾  Save Settings")
        save_btn.setObjectName("btnSuccess")
        save_btn.setMinimumHeight(38)
        save_btn.clicked.connect(self._save_settings)
        layout.addWidget(save_btn, 0, Qt.AlignmentFlag.AlignRight)
        layout.addStretch()
        return w

    def _stat_card(self, title: str, value: str, accent: str) -> QFrame:
        card = QFrame()
        card.setObjectName("statCard")
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 10, 14, 10)
        vl.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        v = QLabel(value)
        v.setObjectName("cardValue")
        v.setStyleSheet(f"color: {accent};")
        vl.addWidget(t)
        vl.addWidget(v)
        card._value_lbl = v   # type: ignore[attr-defined]
        return card

    # ── theme ──────────────────────────────────────────────────────────────────
    def _apply_dark_theme(self) -> None:
        self.setStyleSheet("""
        QMainWindow, QWidget { background: #1e1e1e; color: #d4d4d4; }

        #header {
            background: #252526;
            border-bottom: 1px solid #3e3e3e;
        }
        #title {
            font-size: 18px; font-weight: bold; color: #4ec9b0;
            letter-spacing: 1px;
        }
        #statusBadge {
            background: #37373d; color: #9cdcfe;
            padding: 4px 12px; border-radius: 12px;
            font-size: 12px;
        }

        QTabWidget::pane  { border: none; }
        QTabBar::tab      { background: #2d2d30; color: #858585; padding: 8px 20px; border: none; }
        QTabBar::tab:selected { background: #1e1e1e; color: #d4d4d4; border-top: 2px solid #4ec9b0; }
        QTabBar::tab:hover    { background: #252526; color: #d4d4d4; }

        #statCard       { background: #252526; border: 1px solid #3e3e3e; border-radius: 8px; }
        #cardTitle      { color: #858585; font-size: 11px; }
        #cardValue      { font-size: 20px; font-weight: bold; }

        #actionGroup    { border: 1px solid #3e3e3e; border-radius: 8px; padding: 8px; }
        #actionGroup::title { color: #858585; }

        QPushButton {
            border: none; border-radius: 6px;
            padding: 8px 16px; font-size: 13px; font-weight: 600;
        }
        #btnPrimary   { background: #0e639c; color: #fff; }
        #btnPrimary:hover { background: #1177bb; }
        #btnSecondary { background: #37373d; color: #d4d4d4; }
        #btnSecondary:hover { background: #4a4a50; }
        #btnSuccess   { background: #4a7c4e; color: #fff; }
        #btnSuccess:hover { background: #5a9060; }
        #btnWarning   { background: #7c6a2c; color: #fff; }
        #btnWarning:hover { background: #9a8430; }
        #btnInfo      { background: #4e6a7c; color: #fff; }
        #btnInfo:hover { background: #5d7d8e; }
        #btnDanger    { background: #7c3333; color: #fff; }
        #btnDanger:hover { background: #9a3e3e; }
        #btnDanger:disabled { background: #3e3e3e; color: #555; }

        QGroupBox { border: 1px solid #3e3e3e; border-radius: 8px; margin-top: 12px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #858585; }

        QProgressBar { background: #3e3e3e; border-radius: 4px; }
        QProgressBar::chunk { background: #4ec9b0; border-radius: 4px; }

        #logView {
            background: #0c0c0c; color: #cccccc;
            border: 1px solid #3e3e3e; border-radius: 6px;
        }

        QScrollBar:vertical { background: #2d2d30; width: 10px; }
        QScrollBar::handle:vertical { background: #555; border-radius: 5px; }

        QSpinBox, QTextEdit {
            background: #3c3c3c; color: #d4d4d4;
            border: 1px solid #555; border-radius: 4px; padding: 4px;
        }
        QCheckBox { spacing: 8px; }
        QCheckBox::indicator { width: 16px; height: 16px; }
        QCheckBox::indicator:unchecked { background: #3c3c3c; border: 1px solid #555; border-radius: 3px; }
        QCheckBox::indicator:checked   { background: #4ec9b0; border-radius: 3px; }

        #pkgList { background: #252526; border: 1px solid #3e3e3e; border-radius: 6px; }
        QListWidget::item:selected { background: #37373d; }

        #toolbar { background: #252526; border-top: 1px solid #3e3e3e; }
        """)

    # ── logic ──────────────────────────────────────────────────────────────────
    def _run_task(self, task: str) -> None:
        if self.worker and self.worker.isRunning():
            self._pending_task = task
            self._append_log("⚠ Another task is running. Will run after it finishes.", "warn")
            return
        self.worker = UpdateWorker(task, self.config)
        self.worker.output_line.connect(self._append_log)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.packages_found.connect(self._on_packages_found)
        self.worker.finished.connect(self._on_task_finished)
        self.btn_abort.setEnabled(True)
        self._set_status(f"Running: {task}", "#ce9178")
        self.worker.start()

    def _abort_task(self) -> None:
        if self.worker:
            self.worker.abort()
        self.btn_abort.setEnabled(False)

    def _on_packages_found(self, pkgs: list) -> None:
        self.pkg_list = pkgs
        self.pkg_list_widget.clear()
        for p in pkgs:
            self.pkg_list_widget.addItem(QListWidgetItem(f"  📦  {p}"))
        self.card_packages._value_lbl.setText(str(len(pkgs)))   # type: ignore
        now = datetime.now().strftime("%H:%M %d/%m/%Y")
        self.card_last._value_lbl.setText(now)                   # type: ignore
        self.config["last_check"] = now
        if pkgs and self.config.get("notify_on_updates"):
            self._notify("Updates Available", f"{len(pkgs)} package(s) ready to update.")

    def _on_task_finished(self, success: bool, summary: str) -> None:
        self.btn_abort.setEnabled(False)
        colour = "#4ec9b0" if success else "#f44747"
        self._append_log(f"{'✅' if success else '❌'} {summary}",
                         "ok" if success else "error")
        self._set_status("Ready", colour)
        self.progress_bar.setValue(0)

        if self.worker and "update" in self.worker.task:
            now = datetime.now().strftime("%H:%M %d/%m/%Y")
            self.card_updated._value_lbl.setText(now)            # type: ignore
            self.config["last_update"] = now
            save_config(self.config)

        if success:
            self.card_status._value_lbl.setText(                 # type: ignore
                "Up to date" if not self.pkg_list else "Updates found"
            )
            if self.config.get("notify_on_complete"):
                self._notify("Gentoo Updater", summary)

        if self._pending_task:
            t, self._pending_task = self._pending_task, None
            self._run_task(t)

    def _append_log(self, line: str, level: str = "info") -> None:
        colours = {
            "cmd":   "#569cd6",
            "error": "#f44747",
            "warn":  "#dcdcaa",
            "ok":    "#4ec9b0",
            "info":  "#cccccc",
        }
        col  = colours.get(level, "#cccccc")
        ts   = datetime.now().strftime("%H:%M:%S")
        html = (f'<span style="color:#555">[{ts}]</span> '
                f'<span style="color:{col}">{line}</span>')
        self.log_view.append(html)
        # Qt6 fully-qualified enum
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _set_status(self, text: str, colour: str = "#d4d4d4") -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(
            f"background:#37373d;color:{colour};"
            "padding:4px 12px;border-radius:12px;font-size:12px;"
        )

    @staticmethod
    def _notify(title: str, body: str) -> None:
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Gentoo Updater",
                 "-i", "system-software-update", title, body]
            )
        except FileNotFoundError:
            pass

    def _toggle_auto_update(self, enabled: bool) -> None:
        self.config["auto_update"] = enabled
        save_config(self.config)
        self._append_log(
            f"Automatic updates {'enabled' if enabled else 'disabled'}.", "info"
        )

    def _save_settings(self) -> None:
        self.config.update({
            "check_interval_hours": self.spin_interval.value(),
            "auto_sync":            self.chk_sync.isChecked(),
            "update_world":         self.chk_world.isChecked(),
            "preserved_rebuild":    self.chk_prebuilt.isChecked(),
            "deep_clean":           self.chk_deep.isChecked(),
            "news_check":           self.chk_news.isChecked(),
            "emerge_opts":          self.emerge_opts_edit.toPlainText().strip(),
            "notify_on_updates":    self.chk_notify_found.isChecked(),
            "notify_on_complete":   self.chk_notify_complete.isChecked(),
        })
        save_config(self.config)
        self._reset_timer()
        self._append_log("Settings saved.", "ok")
        QMessageBox.information(self, "Saved", "Settings saved successfully.")

    def _reset_timer(self) -> None:
        h = self.config.get("check_interval_hours", 6)
        self.timer.start(h * 3600 * 1000)
        logger.info("Next check in %d hour(s).", h)

    def closeEvent(self, event) -> None:          # type: ignore[override]
        event.ignore()
        self.hide()


# ── System tray ───────────────────────────────────────────────────────────────
class TrayIcon(QSystemTrayIcon):
    def __init__(self, window: MainWindow) -> None:
        icon = QIcon.fromTheme(
            "system-software-update",
            QIcon.fromTheme("emblem-system"),
        )
        super().__init__(icon)
        self.window = window
        self._build_menu()
        self.activated.connect(self._on_activate)

    def _build_menu(self) -> None:
        menu = QMenu()
        for label, slot in (
            ("Open Updater",      self.window.show),
            (None,                None),
            ("Check for Updates", lambda: self.window._run_task("check")),
            ("Sync Portage",      lambda: self.window._run_task("sync")),
            ("Update System Now", lambda: self.window._run_task("update")),
            (None,                None),
            ("Quit",              QApplication.quit),
        ):
            if label is None:
                menu.addSeparator()
            else:
                act = QAction(label, menu)
                act.triggered.connect(slot)
                menu.addAction(act)
        self.setContextMenu(menu)
        self.setToolTip("Gentoo Auto Updater")

    def _on_activate(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Qt6: fully-qualified enum
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.window.show()
            self.window.raise_()


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Gentoo Auto Updater")
    app.setQuitOnLastWindowClosed(False)

    config = load_config()
    window = MainWindow(config)

    tray = TrayIcon(window)
    tray.show()
    # Qt6 fully-qualified enum
    tray.showMessage(
        "Gentoo Auto Updater",
        "Running in system tray. Double-click to open.",
        QSystemTrayIcon.MessageIcon.Information,
        3000,
    )

    window.show()
    # Qt6: exec() not exec_()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
