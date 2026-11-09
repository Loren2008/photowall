import sys

from photo_wall.bootstrap import ensure_runtime

ensure_runtime()

from photo_wall.app import main

if __name__ == "__main__":
    raise SystemExit(main())
