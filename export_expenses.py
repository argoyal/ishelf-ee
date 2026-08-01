"""Backward-compatible entry point.

The tool now lives in the `ishelf_ee` package. Prefer the installed `ee` command
(`pip install ishelf-ee`) or `python -m ishelf_ee ...`. This shim keeps the old
`python3 export_expenses.py ...` invocation working from the repo.
"""
import sys

from ishelf_ee import main

if __name__ == "__main__":
    sys.exit(main())
