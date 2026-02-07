from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from core.engine_manager import EngineManager
from core.settings_manager import SettingsManager
from logger import Logger
from ui.pages import AppLauncherPage, DashboardPage, VPNManagerPage, AppRoutingPage, SettingsPage


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, engine: EngineManager, settings: SettingsManager, logger: Optional[Logger] = None):
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.logger = logger or Logger()

        self.setWindowTitle("Umbra v2")
        self.setMinimumSize(1050, 720)

        self._tray: Optional[QtWidgets.QSystemTrayIcon] = None
        self._build_ui()
        self._setup_tray()
        self._apply_close_behavior()

    # ---------------------
    # UI
    # ---------------------

    def _build_ui(self):
        root = QtWidgets.QWidget()
        root_layout = QtWidgets.QHBoxLayout(root)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(12)

        # Left Nav
        nav = QtWidgets.QFrame()
        nav.setFixedWidth(220)
        nav_layout = QtWidgets.QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(10)

        logo = QtWidgets.QLabel("Umbra")
        font = logo.font()
        font.setPointSize(16)
        font.setBold(True)
        logo.setFont(font)
        nav_layout.addWidget(logo)

        self.btn_dashboard = QtWidgets.QPushButton("Dashboard")
        self.btn_launcher = QtWidgets.QPushButton("App Launcher")
        self.btn_vpn = QtWidgets.QPushButton("VPN Manager")
        self.btn_route = QtWidgets.QPushButton("App Routing")
        self.btn_settings = QtWidgets.QPushButton("Settings")

        for b in (self.btn_dashboard, self.btn_launcher, self.btn_vpn, self.btn_route, self.btn_settings):
            b.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
            b.setMinimumHeight(44)

        nav_layout.addWidget(self.btn_dashboard)
        nav_layout.addWidget(self.btn_launcher)
        nav_layout.addWidget(self.btn_vpn)
        nav_layout.addWidget(self.btn_route)
        nav_layout.addWidget(self.btn_settings)
        nav_layout.addStretch(1)

        # Content
        self.stack = QtWidgets.QStackedWidget()

        self.page_dashboard = DashboardPage(
            self.engine,
            self.settings,
            go_to_settings_cb=self._go_settings,
            is_refresh_paused_cb=self._is_refresh_paused,
        )
        self.page_launcher = AppLauncherPage(self.engine, self.settings, is_refresh_paused_cb=self._is_refresh_paused)
        self.page_vpn = VPNManagerPage(self.engine, self.settings)
        self.page_route = AppRoutingPage(
            self.engine,
            self.settings,
            is_refresh_paused_cb=self._is_refresh_paused,
        )
        self.page_settings = SettingsPage(self.engine, self.settings)

        self.stack.addWidget(self.page_dashboard)
        self.stack.addWidget(self.page_launcher)
        self.stack.addWidget(self.page_vpn)
        self.stack.addWidget(self.page_route)
        self.stack.addWidget(self.page_settings)

        root_layout.addWidget(nav)
        root_layout.addWidget(self.stack, 1)

        self.setCentralWidget(root)

        # Wiring
        self.btn_dashboard.clicked.connect(lambda: self.stack.setCurrentWidget(self.page_dashboard))
        self.btn_launcher.clicked.connect(lambda: self.stack.setCurrentWidget(self.page_launcher))
        self.btn_vpn.clicked.connect(lambda: self.stack.setCurrentWidget(self.page_vpn))
        self.btn_route.clicked.connect(lambda: self.stack.setCurrentWidget(self.page_route))
        self.btn_settings.clicked.connect(lambda: self.stack.setCurrentWidget(self.page_settings))

        self._build_menu()

    def _go_settings(self):
        self.stack.setCurrentWidget(self.page_settings)

    def _build_menu(self):
        menu = self.menuBar()

        view_menu = menu.addMenu("View")
        view_menu.addAction("Dashboard", lambda: self.stack.setCurrentWidget(self.page_dashboard))
        view_menu.addAction("App Launcher", lambda: self.stack.setCurrentWidget(self.page_launcher))
        view_menu.addAction("VPN Manager", lambda: self.stack.setCurrentWidget(self.page_vpn))
        view_menu.addAction("App Routing", lambda: self.stack.setCurrentWidget(self.page_route))
        view_menu.addAction("Settings", lambda: self.stack.setCurrentWidget(self.page_settings))

        actions_menu = menu.addMenu("Actions")
        actions_menu.addAction("Start Engine", self.engine.start_engine)
        actions_menu.addAction("Stop Engine", self.engine.stop_engine)
        actions_menu.addAction("Refresh App List", self.page_route.refresh_now)

    # ---------------------
    # Tray / Close behavior
    # ---------------------

    def _setup_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
        tray = QtWidgets.QSystemTrayIcon(icon, self)
        tray.setToolTip("Umbra v2")

        menu = QtWidgets.QMenu()
        act_show = menu.addAction("Show")
        act_start = menu.addAction("Start Engine")
        act_stop = menu.addAction("Stop Engine")
        menu.addSeparator()
        act_apps = menu.addAction("Refresh App List")
        act_settings = menu.addAction("Open Settings")
        menu.addSeparator()
        act_exit = menu.addAction("Exit")

        act_show.triggered.connect(self._tray_show)
        act_start.triggered.connect(self.engine.start_engine)
        act_stop.triggered.connect(self.engine.stop_engine)
        act_apps.triggered.connect(self.page_route.refresh_now)
        act_settings.triggered.connect(self._go_settings)
        act_exit.triggered.connect(self._tray_exit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()

        self._tray = tray

    def _apply_close_behavior(self):
        # reads settings; called on init and when user changes behavior
        pass

    def _is_refresh_paused(self) -> bool:
        ui = (self.settings.data.get("ui", {}) or {})
        pause_when_min = bool(ui.get("pause_refresh_when_minimized", True))
        if not pause_when_min:
            return False
        return self.isMinimized() or not self.isVisible()

    def _tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason):
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            self._tray_show()

    def _tray_show(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _tray_exit(self):
        self._cleanup_before_exit()
        QtWidgets.QApplication.quit()

    def _cleanup_before_exit(self):
        try:
            self.page_dashboard.shutdown()
        except Exception:
            pass
        try:
            self.engine.shutdown()
        except Exception:
            pass

    def closeEvent(self, event: QtGui.QCloseEvent):
        ui = (self.settings.data.get("ui", {}) or {})
        tray_enabled = bool(ui.get("tray_enabled", True))
        close_action = str(ui.get("close_action", "minimize_to_tray"))

        if tray_enabled and close_action == "minimize_to_tray" and self._tray is not None:
            event.ignore()
            self.hide()
            try:
                self._tray.showMessage("Umbra", "Running in system tray. Right-click tray icon -> Exit to close.",
                                       QtWidgets.QSystemTrayIcon.MessageIcon.Information, 2500)
            except Exception:
                pass
            return

        self._cleanup_before_exit()
        event.accept()
