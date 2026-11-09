"""第一次运行时，按 requirements.txt 把依赖装进项目自己的虚拟环境。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
VENV = ROOT / ".venv"


def ensure_runtime() -> None:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        _install_and_restart()


def _install_and_restart() -> None:
    if not REQUIREMENTS.is_file():
        _fail("找不到 requirements.txt。")
    python = _venv_python()
    if python.exists() and Path(sys.executable).resolve() == python.resolve():
        _fail("依赖没有装好，界面库仍然无法导入。")
    sys.stderr.write("还没有安装界面库，正在按 requirements.txt 安装…\n")
    try:
        if not python.exists():
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
        subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    except (OSError, subprocess.CalledProcessError):
        manual = (
            f"{python} {Path(sys.argv[0]).name}"
            if python.exists()
            else f"python3 {Path(sys.argv[0]).name}"
        )
        _fail(
            "自动安装没有完成。可以在项目目录里手动执行：\n"
            f"  python3 -m venv .venv\n"
            f"  {python} -m pip install -r requirements.txt\n"
            f"  {manual}"
        )
    os.execv(str(python), [str(python), *sys.argv])


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _fail(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    raise SystemExit(1)
