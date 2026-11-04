r"""Blueprint-layer unit tests (stdlib unittest).

Run from the repo root with the project venv:
    .\venv\Scripts\python.exe -m unittest discover -s tests -v
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
