"""Entry point for survey-studio.exe (PyInstaller, windowed: no console window)."""
import sys

from emergence_kit.studio.app import main

if __name__ == "__main__":
    sys.exit(main())
