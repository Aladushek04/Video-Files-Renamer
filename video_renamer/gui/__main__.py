"""`python -m video_renamer.gui` — прямой запуск без автообновления."""

from __future__ import annotations

import sys

from video_renamer.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
