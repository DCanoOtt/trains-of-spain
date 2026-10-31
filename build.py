"""Genera el ejecutable con PyInstaller:  python3 build.py
Necesita:  pip install pyinstaller certifi   (hay que ejecutarlo en cada sistema operativo: no se puede cruzar)."""
import os, subprocess, sys
sep = ";" if os.name == "nt" else ":"
cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--name", "MonitorTrenesRenfe",
       "--add-data", "index.html" + sep + ".", "--collect-data", "certifi"]
if os.path.exists("data/rails.json"):
    cmd += ["--add-data", "data/rails.json" + sep + "data"]
cmd.append("server.py")
sys.exit(subprocess.call(cmd))
