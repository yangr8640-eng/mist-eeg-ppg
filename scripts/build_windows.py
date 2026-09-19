"""Build a replaceable-library Windows distribution and ZIP, without uploading data."""
from __future__ import annotations
import importlib.metadata
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--windowed", "--name", "MIST-EEG-PPG", "--paths", str(root),
        "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "matplotlib", "--exclude-module", "scipy", "--exclude-module", "tkinter",
        "--collect-submodules", "bleak", "--collect-submodules", "winrt", str(root / "scripts" / "launcher.py")],
        cwd=root, check=True)
    output = root / "dist" / "MIST-EEG-PPG"
    for filename in ("README.md", "LICENSE"):
        shutil.copy2(root / filename, output / filename)
    shutil.copytree(root / "docs", output / "docs", dirs_exist_ok=True)
    notices = output / "third_party_licenses"
    notices.mkdir(exist_ok=True)
    dependencies = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        dependencies.append(f"{name}=={dist.version}")
        for file in dist.files or []:
            if "license" in str(file).lower() or "copying" in file.name.lower():
                source = Path(dist.locate_file(file))
                if source.is_file():
                    destination = notices / name / str(file).replace("..", "_")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, notices / "Python-LICENSE.txt")
    (output / "dependencies.txt").write_text("\n".join(sorted(dependencies)) + "\n", encoding="utf-8")
    (output / "Start-Simulation.cmd").write_text('@echo off\ncd /d "%~dp0"\nstart "" "MIST-EEG-PPG.exe" --simulate\n', encoding="ascii")
    archive = shutil.make_archive(str(root / "dist" / "MIST-EEG-PPG-Windows-x64"), "zip", output.parent, output.name)
    print(archive)


if __name__ == "__main__":
    main()
