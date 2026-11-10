"""Bounded, single-host cache shared by the API workers; no user data."""
import fcntl
import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class SchedulePool:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(tempfile.gettempdir()) / f"sofly-schedule-pool-v1-{os.getuid()}")

    def snapshot(self):
        try:
            self.check_directory()
            with (self.directory / "pool.json").open() as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Unable to read shared schedule pool")
            return {}

    def check_directory(self):
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Shared schedule cache directory must be private")

    def update(self, change):
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self.check_directory()
            with (self.directory / "pool.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = self.snapshot()
                result = change(state)
                if result is None:
                    return None
                with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as handle:
                    temporary = Path(handle.name)
                    try:
                        json.dump(state, handle, separators=(",", ":"))
                        handle.flush()
                        temporary.replace(self.directory / "pool.json")
                    finally:
                        temporary.unlink(missing_ok=True)
                return result
        except BlockingIOError:
            return None
        except OSError:
            logger.warning("Unable to update shared schedule pool")
            return None

    def claim(self):
        def change(state):
            now = time.time()
            if now < state.get("next_batch", 0):
                return None
            # A crashed worker releases the batch automatically; publication is
            # fenced by this lease and an old worker cannot overwrite a new one.
            state["next_batch"] = now + 65
            state["lease"] = now
            state["cursor"] = state.get("cursor", -1) + 1
            return now, state["cursor"], state.get("entries", {})
        return self.update(change)

    def publish(self, lease, entries, backoff=False):
        def change(state):
            if state.get("lease") != lease or time.time() >= state["next_batch"]:
                return None
            now = time.time()
            merged = {k: v for k, v in state.get("entries", {}).items() if v[0] > now}
            merged.update(entries)
            state["entries"] = dict(list(merged.items())[-512:])
            if backoff:
                state["next_batch"] = max(state["next_batch"], now + 180)
            return True
        return self.update(change)
