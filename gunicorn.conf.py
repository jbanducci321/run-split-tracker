"""Gunicorn settings, picked up automatically from this folder (the Procfile
still sets bind and workers).

Only logging is configured here: gunicorn's own informational lines go to
stdout and its warnings/errors to stderr - Railway shows every stderr line in
red, so red should mean a real problem. The app's logs are set up the same
way by app.configure_logging(), which is why the root logger gets no handlers
here.
"""
import logging


class BelowWarning(logging.Filter):
    def filter(self, record):
        return record.levelno < logging.WARNING


# Gunicorn merges this over its defaults one top-level key at a time, so every
# key it has defaults for (root, loggers, handlers, formatters) is spelled out.
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"below_warning": {"()": BelowWarning}},
    "formatters": {
        "gunicorn": {  # gunicorn's usual format
            "format": "%(asctime)s [%(process)d] [%(levelname)s] %(message)s",
            "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
        },
    },
    "handlers": {
        "gunicorn_stdout": {
            "class": "logging.StreamHandler", "formatter": "gunicorn",
            "stream": "ext://sys.stdout", "filters": ["below_warning"],
        },
        "gunicorn_stderr": {
            "class": "logging.StreamHandler", "formatter": "gunicorn",
            "stream": "ext://sys.stderr", "level": "WARNING",
        },
    },
    "root": {"level": "INFO", "handlers": []},  # app.configure_logging() adds the app's handlers
    "loggers": {
        "gunicorn.error": {"level": "INFO", "handlers": ["gunicorn_stdout", "gunicorn_stderr"], "propagate": False},
        "gunicorn.access": {"level": "INFO", "handlers": ["gunicorn_stdout"], "propagate": False},
    },
}
