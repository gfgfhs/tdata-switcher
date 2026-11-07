#!/usr/bin/env python3
"""Консольная точка входа: без аргументов открывает текстовое меню (для tdata_switcher_cli.exe)."""

import sys

from tdata_switcher import main, report_error

if __name__ == "__main__":
    try:
        sys.exit(main(default_mode="cli"))
    except KeyboardInterrupt:
        print("\nОтмена")
    except Exception as e:
        report_error(f"{type(e).__name__}: {e}")
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nEnter...")
