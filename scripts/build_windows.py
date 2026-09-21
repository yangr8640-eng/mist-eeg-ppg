"""Build a replaceable-library Windows distribution and ZIP, without uploading data."""
from __future__ import annotations
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    # DLL dependency analysis searches PATH. Third-party tools (e.g. Poppler)
    # can provide an incompatible, version-suffixed ICU that shadows Windows'
    # unversioned ICU required by Qt. Restrict only this build subprocess PATH.
    build_env = dict(os.environ)
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    build_env["PATH"] = os.pathsep.join(map(str, [Path(sys.executable).parent,
        Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs", windows / "System32", windows]))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--windowed", "--name", "MIST-EEG-PPG", "--paths", str(root),
        "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "matplotlib", "--exclude-module", "scipy", "--exclude-module", "tkinter",
        "--collect-submodules", "bleak", "--collect-submodules", "winrt", str(root / "scripts" / "launcher.py")],
        cwd=root, env=build_env, check=True)
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
    # Validate the actual frozen executable, not only source imports. No ZIP
    # should be published if DLL collection or hidden imports break startup.
    with tempfile.TemporaryDirectory(prefix="mist-frozen-check-") as check_dir:
        subprocess.run([str(output / "MIST-EEG-PPG.exe"), "--self-test", check_dir,
            "--self-test-hidden"], cwd=output, env=build_env, check=True, timeout=60)
        report = json.loads((Path(check_dir) / "self_test_report.json").read_text(encoding="utf-8"))
        if report.get("status") != "passed":
            raise RuntimeError(f"Packaged self-test failed: {report}")
        # Publish reproducible checks, without embedding a local username/path.
        report.pop("session_path", None)
        report.pop("screenshots", None)
        for session in report.get("sessions", []):
            session.pop("session_path", None)
        (output / "build-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = shutil.make_archive(str(root / "dist" / "MIST-EEG-PPG-Windows-x64"), "zip", output.parent, output.name)
    print(archive)


if __name__ == "__main__":
    main()
