import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "app.log"

IGNORE_ACCESS_PATHS: tuple[str, ...] = ("/rci",)


class IgnorePathsFilter(logging.Filter):
    """Drop uvicorn access records whose request path starts with any prefix in `paths`."""

    def __init__(self, paths: tuple[str, ...]):
        super().__init__()
        self.paths = paths

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn access records have args=(client, method, path, version, status)
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = args[2]
        if not isinstance(path, str):
            return True
        return not any(path.startswith(p) for p in self.paths)


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)

    for name in ("uvicorn.error", "uvicorn.access", "app"):
        logger = logging.getLogger(name)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)

    # Mirror our own app logs to stderr too so they show up in the console / debugger.
    logging.getLogger("app").addHandler(stream_handler)

    logging.getLogger("uvicorn.access").addFilter(IgnorePathsFilter(IGNORE_ACCESS_PATHS))
