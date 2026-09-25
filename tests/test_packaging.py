"""The PEP 517 build must not import the Hermes gateway at metadata time."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import zipfile


def test_isolated_wheel_build(tmp_path):
    root = pathlib.Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(tmp_path), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = tuple(tmp_path.glob("hermes_plugin_carbonvoice-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "hermes_plugin_carbonvoice/registration.py" in archive.namelist()
        entry = next(name for name in archive.namelist() if name.endswith("entry_points.txt"))
        assert "carbonvoice = hermes_plugin_carbonvoice" in archive.read(entry).decode()
