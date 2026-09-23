#!/usr/bin/env python3
"""
validate_dev.py — start the Vite dev server, verify it serves the page and
the forecast data, then kill the whole process tree. Never leaves a server
running (uses taskkill /T /F and verifies the port is closed afterwards).
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
NODE_DIR = Path(os.path.expandvars(
    r"%LOCALAPPDATA%\Programs\kimi-desktop\resources\resources\runtime"))
HOST, PORT = "127.0.0.1", 5199
BASE = f"http://{HOST}:{PORT}"


def fetch(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    env = os.environ.copy()
    env["PATH"] = str(NODE_DIR) + os.pathsep + env["PATH"]
    log_file = open(ROOT / "backend" / "vite_debug.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        f'"{NODE_DIR / "npm.cmd"}" run dev -- --host {HOST} --port {PORT}',
        cwd=FRONTEND, shell=True, env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    print(f"dev server starting (pid {proc.pid}) …")
    try:
        status = None
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                status, body = fetch(BASE + "/")
                break
            except Exception:
                time.sleep(0.5)
        if status != 200:
            print("FAIL: dev server did not come up within 60 s")
            return 1
        print(f"GET /                 -> {status} ({len(body)} bytes)")
        for path in ("/src/main.js", "/data/forecast.json", "/data/DNK.geo.json"):
            s, b = fetch(BASE + path)
            print(f"GET {path:19s} -> {s} ({len(b)} bytes)")
            if s != 200:
                print(f"FAIL: {path} returned {s}")
                return 1
        print("PASS: page, module script and forecast data all served")
        return 0
    finally:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        log_file.close()
        time.sleep(2)
        try:
            fetch(BASE + "/", timeout=2)
            print("WARNING: port still answering after kill!")
        except Exception:
            print("dev server stopped; port 5199 is free")


if __name__ == "__main__":
    sys.exit(main())
