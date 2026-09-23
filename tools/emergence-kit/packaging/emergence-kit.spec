# PyInstaller build for Windows: two programs sharing one folder.
#
#   emergence-kit.exe   command line (console)      - IT, scripting, the hub worker
#   survey-studio.exe   the desktop app (windowed)  - what ecologists open
#
#   python -m PyInstaller --noconfirm --clean packaging\emergence-kit.spec
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "survey-studio.ico")
HIDDEN = collect_submodules("emergence_kit") + ["numpy", "tkinter", "tkinter.filedialog"]
DATAS = [(os.path.join(ROOT, "emergence_kit", "studio", "static"),
          os.path.join("emergence_kit", "studio", "static"))]

cli = Analysis([os.path.join(SPECPATH, "launcher.py")], pathex=[ROOT],
               hiddenimports=HIDDEN, datas=DATAS)
app = Analysis([os.path.join(SPECPATH, "studio_launcher.py")], pathex=[ROOT],
               hiddenimports=HIDDEN, datas=DATAS)

cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True,
              name="emergence-kit", console=True, icon=ICON)
app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True,
              name="survey-studio", console=False, icon=ICON)

COLLECT(cli_exe, cli.binaries, cli.datas, app_exe, app.binaries, app.datas,
        name="emergence-kit")
