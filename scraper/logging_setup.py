import logging
import logging.handlers
from pathlib import Path


def configure_logging(level: str, log_file: str) -> None:
    """Настраивает root-логгер: stdout + ротируемый файл.

    Пишем в файл всё (DEBUG), в консоль — на уровне level.
    """
    level_num = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(level_num)
    console.setFormatter(fmt)
    root.addHandler(console)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # urllib3 на DEBUG захламляет — держим на INFO всегда.
    logging.getLogger("urllib3").setLevel(logging.INFO)


class YtdlpLoggerAdapter:
    """Адаптер: перехватывает логи yt-dlp в наш logger.

    yt-dlp очень разговорчив (Destination/Merger/progress и т.п.) — всё это DEBUG.
    Предупреждения и ошибки пропускаем на соответствующие уровни.
    """

    def __init__(self, logger: logging.Logger):
        self._log = logger

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            self._log.debug(msg[8:])
        else:
            self._log.debug(msg)

    def info(self, msg: str) -> None:
        self._log.debug(msg)

    def warning(self, msg: str) -> None:
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        self._log.error(msg)
