from __future__ import annotations

import os
import sys
import traceback

from PySide6 import QtWidgets

from logger import Logger
from core.settings_manager import SettingsManager
from core.engine_manager import EngineManager
from ui.main_window import MainWindow
from ui.pages import FirstRunWizard


def load_style(app: QtWidgets.QApplication, path: str = "style.css"):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                app.setStyleSheet(f.read())
    except Exception:
        pass


def main():
    # Ensure working dir is project root when launched from anywhere
    try:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass

    logger = Logger(log_path=os.path.join("logs", "umbra.log"))

    try:
        settings = SettingsManager(path=os.path.join("configs", "settings.json"))
        engine = EngineManager(settings, logger=logger)

        app = QtWidgets.QApplication(sys.argv)
        app.setApplicationName("Umbra")
        load_style(app)

        # First run wizard (manual + opt-in)
        if settings.is_first_run_pending():
            dlg = FirstRunWizard(settings)
            dlg.exec()

        window = MainWindow(engine, settings, logger=logger)
        window.show()

        rc = app.exec()
        return rc
    except Exception as e:
        print("Umbra failed to start.\n")
        print("Error:", e)
        print("\nTraceback:")
        traceback.print_exc()
        input("\nPress Enter to exit...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
