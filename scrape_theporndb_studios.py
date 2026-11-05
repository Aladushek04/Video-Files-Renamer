#!/usr/bin/env python3
"""Скрапер студий ThePornDB (stash-box) с чекпоинтом и безопасным merge."""
import sys

from scrape_studios import THEPORNDB_SOURCE, main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], source=THEPORNDB_SOURCE))
