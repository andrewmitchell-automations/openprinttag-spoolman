"""The poller and the state the web interface shows. A lock keeps one Connect call at a time,
because two simultaneous token refreshes could invalidate a rotated refresh token."""

import logging
import threading
from collections import deque
from dataclasses import replace

from .config import ConfigError, Settings, load_settings
from .connect import ConnectAuthError, ConnectClient, TokenStore, Tokens, jwt_expiry
from .spoolman import SpoolmanClient
from .sync import Bridge, LoadedSpool, State, build_link_changes, rank_spools_for_link

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 1800


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 200):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class Service:
    def __init__(self, env: dict | None = None):
        self.env = env
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.logs = RingBufferHandler()
        self.settings = Settings()
        self.config_error: str | None = None
        self.spoolman: SpoolmanClient | None = None
        self.connect: ConnectClient | None = None
        self.bridge: Bridge | None = None
        self.status: dict[str, dict] = {}
        self.login_email: str | None = None
        self.last_auth_error: str | None = None
        self.reload()

    def reload(self) -> None:
        """Read the settings again and rebuild the clients."""
        with self.lock:
            try:
                self.settings = load_settings(self.env)
                self.connect = ConnectClient(self.settings, TokenStore(self.settings.tokens_path))
                self.spoolman = SpoolmanClient(self.settings) if self.settings.spoolman_url else None
                self.config_error = None
            except ConfigError as e:
                log.error("%s", e)
                self.config_error = str(e)
                self.spoolman = None
            self.bridge = None
            if self.spoolman and self.connect:
                state = State(self.settings.state_path, self.settings.dry_run)
                self.bridge = Bridge(self.connect, self.spoolman, state, self.settings.auto_create)
        self.wake.set()

    @property
    def store(self) -> TokenStore:
        return TokenStore(self.settings.tokens_path)

    def token_status(self) -> dict:
        """What the interface may show about the login. Never the tokens themselves."""
        if not self.store.exists():
            return {"logged_in": False}
        try:
            tokens = self.store.load()
        except ConnectAuthError:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "expires": jwt_expiry(tokens.access_token),
            "has_refresh": bool(tokens.refresh_token),
            "email": self.login_email,
            "error": self.last_auth_error,
        }

    def log_in_to_connect(self, access: str, refresh: str) -> dict:
        """Store the tokens only if Connect accepts them."""
        with self.lock:
            previous = self.store.load() if self.store.exists() else None
            self.store.save(Tokens(access, refresh))
            try:
                me = self.connect.me()
            except Exception:
                if previous:
                    self.store.save(previous)
                else:
                    self.store.path.unlink(missing_ok=True)
                raise
            self.login_email = me.get("email")
            self.last_auth_error = None
        self.wake.set()
        return me

    def poll_once(self) -> bool:
        """Poll every enabled printer. Returns False if any of them failed."""
        with self.lock:
            if not self.bridge or not self.settings.enabled_printers or not self.store.exists():
                return True
            ok = True
            for config in self.settings.enabled_printers:
                entry = self.status.setdefault(config.uuid, {"actions": []})
                entry["config"] = config
                before = len(self.spoolman.actions) and self.spoolman.actions[-1]["time"]
                try:
                    entry.update(self.bridge.poll(config))
                    entry["error"] = None
                    self.last_auth_error = None
                except ConnectAuthError as e:
                    entry["error"] = str(e)
                    self.last_auth_error = str(e)
                    log.error("CONNECT LOGIN FAILED: %s", e)
                    return False
                except Exception as e:
                    entry["error"] = str(e)
                    log.error("Polling %s failed: %s", config.name or config.uuid, e)
                    ok = False
                new = [a for a in self.spoolman.actions if a["time"] > before]
                if new:
                    entry["actions"] = (entry["actions"] + new)[-10:]
            return ok

    def _loop(self) -> None:
        failures = 0
        while not self.stopping.is_set():
            self.wake.clear()
            if self.poll_once():
                failures = 0
                delay = self.settings.poll_interval
            else:
                failures += 1
                delay = min(self.settings.poll_interval * 2**failures, MAX_BACKOFF_SECONDS)
                log.info("Next attempt in %d s", delay)
            self.wake.wait(delay)

    def start(self) -> None:
        log.info(
            "Starting; polling every %d s, DRY_RUN=%s, AUTO_CREATE=%s",
            self.settings.poll_interval, self.settings.dry_run, self.settings.auto_create,
        )
        self.thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def poller_alive(self) -> bool:
        return self.thread is None or self.thread.is_alive()

    def find_loaded(self, uuid: str) -> tuple[LoadedSpool, str] | None:
        for entry in self.status.values():
            for tool in entry.get("tools", []):
                if tool["spool"].uuid == uuid:
                    return tool["spool"], entry["config"].location
        return None

    def _other_locations(self, loaded: LoadedSpool) -> tuple[str, ...]:
        return tuple(
            entry["config"].location
            for entry in self.status.values()
            if entry["config"].location and all(tool["spool"].uuid != loaded.uuid for tool in entry.get("tools", []))
        )

    def _loaded_now(self) -> set[str]:
        return {tool["spool"].uuid for entry in self.status.values() for tool in entry.get("tools", [])}

    def link_candidates(self, loaded: LoadedSpool) -> list[dict]:
        with self.lock:
            return rank_spools_for_link(loaded, self.spoolman.list_spools(), self._other_locations(loaded), self._loaded_now())

    def _loaded(self, uuid: str) -> tuple[LoadedSpool, str]:
        found = self.find_loaded(uuid)
        if not found:
            raise ValueError("That spool is no longer loaded")
        return found

    def link_review(self, uuid: str, spool_id: int) -> dict:
        with self.lock:
            loaded, _ = self._loaded(uuid)
            spools = self.spoolman.list_spools()
            spool = next((s for s in spools if s["id"] == spool_id), None)
            if spool is None:
                raise ValueError(f"Spool {spool_id} was not found")
            filament_id = (spool.get("filament") or {}).get("id")
            shared = sum(
                1
                for other in spools
                if other["id"] != spool_id and filament_id is not None and (other.get("filament") or {}).get("id") == filament_id
            )
            return {"loaded": loaded, "spool": spool, "shared": shared, "changes": build_link_changes(loaded, spool, shared)}

    def _capture(self, action) -> list[str]:
        self.spoolman.captured.clear()
        action()
        calls = list(self.spoolman.captured)
        self.wake.set()
        return calls

    def link(self, uuid: str, spool_id: int, selected: set[str]) -> list[str]:
        with self.lock:
            loaded, _ = self._loaded(uuid)
            return self._capture(lambda: self.bridge.link_spool(loaded, spool_id, selected, self._loaded_now()))

    def unlink(self, spool_id: int) -> list[str]:
        with self.lock:
            return self._capture(lambda: self.bridge.unlink_spool(spool_id))

    def create_review(self, uuid: str) -> dict:
        with self.lock:
            loaded, location = self._loaded(uuid)
            if not loaded.can_create:
                raise ValueError("Connect did not report enough about this spool to create it")
            return {"loaded": loaded, "location": location, "preview": self.bridge.create_preview(loaded)}

    def create(self, uuid: str, name: str, location: str) -> list[str]:
        with self.lock:
            loaded, _ = self._loaded(uuid)
            named = replace(loaded, material_name=name or loaded.material_name)
            return self._capture(lambda: self.bridge.create_spool(named, location))
