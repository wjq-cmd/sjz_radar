from pathlib import Path
import sys

base = Path(SPECPATH)
root = base / "ErCore"
datas = []
for path in (root / "src/core").glob("*.json"):
    if not path.name.startswith("s2c_"):
        datas.append((str(path), "ErCore/src/core"))
datas.append((str(root / "item_names_cn.json"), "ErCore"))
for path in (root / "web").rglob("*"):
    if path.is_file():
        datas.append((str(path), str(Path("ErCore") / path.parent.relative_to(root))))

a = Analysis([str(base / "gateway_parser.py")],
             pathex=[str(base), str(root / "src"), str(root / "src/core")],
             binaries=[], datas=datas, hiddenimports=[],
             excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "tkinter", "matplotlib", "scipy", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="NvaParser",
          debug=False, strip=False, upx=False, console=True)
