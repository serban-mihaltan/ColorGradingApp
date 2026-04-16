import sys
from PySide6.QtWidgets import QApplication
from .config import APP_TITLE
from .main_window import MainWindow, install_slider_commit_hooks


def main():
    """Start the Qt application, create the main window, and enter the event loop."""
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    win = MainWindow()
    install_slider_commit_hooks(win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
