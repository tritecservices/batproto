"""Entry point for the frozen Windows build (PyInstaller): `emergence-kit.exe <command>`."""
import sys

from emergence_kit.cli import main

if __name__ == "__main__":
    sys.exit(main())
