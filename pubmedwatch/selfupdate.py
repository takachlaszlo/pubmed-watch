"""Self-update. The container downloads the program from GitHub at every start (see compose.yaml), so an update
needs nothing but a restart. Once a day, before the daily run, the scheduler asks GitHub for the newest commit of the
branch; if it differs from the one this process started with, the process exits and Docker's restart policy
(`restart: unless-stopped`) starts the container again, which downloads the new code."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

LEAD = timedelta(minutes=30)  # how long before the daily run the check happens (time for the restart to settle)
_SHA = re.compile(r"[0-9a-f]{40}")

# what this process runs; shown by GET /health
running: dict[str, str] = {"sha": "", "started_at": ""}


def latest_sha(http, repo: str, branch: str) -> str:
    """The newest commit of `branch` on GitHub, or '' if GitHub cannot be asked right now."""
    try:
        text = http.request("GET", f"https://api.github.com/repos/{repo}/commits/{branch}",
                            headers={"Accept": "application/vnd.github.sha"}, attempts=2).text.strip()
    except Exception as exc:
        log.warning("önfrissítés: a GitHub most nem kérdezhető le (%s)", exc)
        return ""
    return text if _SHA.fullmatch(text) else ""


def remember_start(http, update) -> None:
    """Notes the commit this process runs: the one GitHub had a moment ago, when the container downloaded it."""
    running["sha"] = latest_sha(http, update.repo, update.branch) if update.enabled else ""
    running["started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    log.info("futó változat: %s", running["sha"][:7] or "ismeretlen")


def newer_version(http, update) -> str:
    """The commit to restart for, or ''. Never restarts blind: if either side is unknown, nothing happens."""
    if not update.enabled:
        return ""
    latest = latest_sha(http, update.repo, update.branch)
    if not running["sha"]:
        running["sha"] = latest  # unknown since the start: what GitHub has now is what we downloaded
        return ""
    return latest if latest and latest != running["sha"] else ""
