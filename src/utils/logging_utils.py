import datetime
import logging

from logging.handlers import RotatingFileHandler


def create_logger() -> logging.Logger:
    log_formatter = logging.Formatter('[%(levelname)s][%(asctime)s](%(funcName)s:%(lineno)d) %(message)s')
    log_formatter.datefmt = '%m.%d.%Y %H:%M:%S'

    current_timestamp = datetime.datetime.now().strftime("%d.%m.%y_%H%M")
    handler = RotatingFileHandler(filename=f"patching_{current_timestamp}.logs",
                                  mode='a',
                                  maxBytes=5 * 1024 * 1024,
                                  encoding='utf-8')

    handler.setFormatter(log_formatter)
    handler.setLevel(logging.INFO)

    app_logger = logging.getLogger('root')
    app_logger.setLevel(logging.DEBUG)

    app_logger.addHandler(handler)
    return app_logger