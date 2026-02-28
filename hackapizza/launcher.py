import subprocess
import sys
from pathlib import Path

COOK_SERVER_DIR = Path(__file__).parent.parent / "resources" / "cook_server"

def _ensure_cook_server_venv():
    """Install cook_server dependencies and return its python executable path"""
    print("Initializing cook_server venv...")
    subprocess.run(
        ["poetry", "install", "--no-root"],
        cwd=COOK_SERVER_DIR,
        check=True
    )
    result = subprocess.run(
        ["poetry", "env", "info", "--executable"],
        cwd=COOK_SERVER_DIR,
        check=True,
        capture_output=True,
        text=True
    )
    python_path = result.stdout.strip()
    print(f"Using cook_server python: {python_path}")
    return python_path

def _main_args():
    return sys.argv[1:]

def run_with_webapp():
    cook_python = _ensure_cook_server_venv()
    server = subprocess.Popen(
        [cook_python, "-m", "uvicorn", "main:app", "--reload", "--port", "8765"],
        cwd=COOK_SERVER_DIR
    )
    try:
        subprocess.run([sys.executable, "main.py"] + _main_args(), check=True)
    finally:
        server.terminate()

def run_webapp_only():
    cook_python = _ensure_cook_server_venv()
    server = subprocess.Popen(
        [cook_python, "-m", "uvicorn", "main:app", "--reload", "--port", "8765"],
        cwd=COOK_SERVER_DIR
    )
    try:
        server.wait()
    except KeyboardInterrupt:
        print("\nShutting down webapp...")
        server.terminate()

def run_without_webapp():
    subprocess.run([sys.executable, "main.py"] + _main_args(), check=True)