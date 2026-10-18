"""Run the tracker on this computer instead of Railway.

    venv\\Scripts\\python serve_local.py

Starts the app with waitress - a production web server that works on Windows
(gunicorn, which Railway uses, doesn't) - at http://127.0.0.1:8080. A tunnel
(Tailscale Funnel, see LOCAL_HOSTING.md) gives it the public HTTPS address
Overland and your friends use. Stop it with Ctrl+C.

While it runs, Windows is asked not to go to sleep, so tracking keeps working
when you're out on a run. (Closing the laptop lid can still put it to sleep -
see LOCAL_HOSTING.md.)
"""
import ctypes
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # before the app is imported, so it sees .env

PORT = int(os.environ.get("PORT", "8080"))
REQUIRED = [
    "OVERLAND_ACCESS_TOKEN", "ADMIN_PASSWORD", "DISCORD_BOT_TOKEN", "DISCORD_USER_ID",
    "DB_HOST", "DB_NAME", "DB_USERNAME", "DB_PASSWORD",
]


def check_settings():
    """Problems with .env for running as the real server: (stop-the-server errors, warnings)."""
    errors = [f"{name} is missing from .env" for name in REQUIRED if not os.environ.get(name)]
    warnings = []
    if os.environ.get("SETTINGS_SCOPE") != "production":
        warnings.append("SETTINGS_SCOPE isn't 'production' - your goal, target pace and switches saved on Railway"
                        " won't load, and past runs won't get elevation/best efforts filled in")
    if os.environ.get("RECOVER_ACTIVE_RUNS", "").lower() != "true":
        warnings.append("RECOVER_ACTIVE_RUNS isn't 'true' - a run in progress won't survive restarting this server")
    if not os.environ.get("PUBLIC_SITE_URL"):
        warnings.append("PUBLIC_SITE_URL isn't set - DMs will leave out the link to the site")
    return errors, warnings


def keep_awake():
    """Ask Windows not to sleep while this process runs (released automatically when it exits)."""
    if sys.platform != "win32":
        return False
    es_continuous, es_system_required = 0x80000000, 0x00000001
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required))


def main():
    errors, warnings = check_settings()
    for problem in errors:
        print(f"ERROR: {problem}", file=sys.stderr)
    if errors:
        sys.exit("Fix .env and start again (see LOCAL_HOSTING.md).")
    for warning in warnings:
        print(f"Note: {warning}", file=sys.stderr)

    import app  # noqa: E402  - imported after .env is loaded and checked
    from waitress import serve

    # One request at a time, like Railway's single gunicorn worker - the app
    # keeps the run in progress in memory. waitress warns whenever a request
    # has to wait its turn (often, with dashboards refreshing), which is expected here.
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    awake = keep_awake()
    app.logger.info("Local server on http://127.0.0.1:%d%s - Ctrl+C to stop", PORT,
                    " (keeping this computer awake)" if awake else "")
    serve(app.app, host="127.0.0.1", port=PORT, threads=1)


if __name__ == "__main__":
    main()
