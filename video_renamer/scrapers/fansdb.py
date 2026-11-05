#!/usr/bin/env python3
"""Скрапер студий FansDB (stash-box) с чекпоинтом и безопасным merge."""
import sys

from video_renamer.scrapers.stashdb import FANSDB_SOURCE, main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], source=FANSDB_SOURCE))
