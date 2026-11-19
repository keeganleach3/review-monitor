import os
import sys
from pathlib import Path

from .cli import main


def load_env(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from ./.env (see .env.example). Variables already set in the shell win."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and value.strip() and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # model text can contain characters a Windows console can't encode
    load_env()
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head/less and closed early
        sys.exit(0)
