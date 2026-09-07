# Default libraries
import logging
import os
import sys


def configure_logging(logger_name="", log_level=None):
    """
    Configure logging for the specified logger with the given log level,
    falling back to environment variable if not provided.
    """

    if log_level is None:
        log_level = os.getenv("LOG_LEVEL", "INFO")

    logger = logging.getLogger(logger_name)
    logger.setLevel(log_level)
    # Prevent log propagation to avoid double logging
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")
        )
        logger.addHandler(handler)

    return logger
