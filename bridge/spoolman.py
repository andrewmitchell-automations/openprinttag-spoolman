"""Spoolman REST client. All writes pass through _write, which honours DRY_RUN."""

import json
import logging
import ssl
import time
from collections import deque

import httpx

from .config import ConfigError, Settings

log = logging.getLogger(__name__)


class SpoolmanError(Exception):
    pass


def tls_verify(settings: Settings) -> ssl.SSLContext | bool:
    if settings.spoolman_url.startswith("http://"):
        return True
    if not settings.spoolman_verify_tls:
        log.warning("SPOOLMAN_VERIFY_TLS=false: the Spoolman certificate is NOT being verified")
        return False
    if settings.spoolman_ca_bundle:
        try:
            return ssl.create_default_context(cafile=settings.spoolman_ca_bundle)
        except (OSError, ssl.SSLError) as e:
            raise ConfigError(f"Cannot load SPOOLMAN_CA_BUNDLE {settings.spoolman_ca_bundle}: {e}") from e
    return True


def extra_value(entity: dict, key: str):
    """Spoolman stores extra field values as JSON-encoded strings."""
    raw = (entity.get("extra") or {}).get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def encode_extra(value: str) -> str:
    return json.dumps(value)


class SpoolmanClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.dry_run = settings.dry_run
        self.url = settings.spoolman_url
        self.base = f"{settings.spoolman_url}/api/v1"
        self.http = http or httpx.Client(timeout=30, verify=tls_verify(settings))
        self.actions: deque[dict] = deque(maxlen=50)
        self._announced: set[str] = set()
        self.captured: list[str] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        try:
            response = self.http.request(method, f"{self.base}{path}", json=body)
        except httpx.HTTPError as e:
            raise SpoolmanError(f"{method} {path} failed: {e}") from e
        if response.status_code >= 400:
            raise SpoolmanError(f"{method} {path} returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json() if response.content else None
        except ValueError as e:
            raise SpoolmanError(f"{method} {path} did not return JSON; is this Spoolman?") from e

    def _read(self, path: str):
        return self._request("GET", path)

    def _write(self, method: str, path: str, body: dict):
        """Returns the response, or None in dry-run mode."""
        rendered = json.dumps(body, ensure_ascii=False)
        line = f"{method} {path} {rendered}"
        self.captured.append(line)
        if self.dry_run:
            # A dry run repeats the same intent every poll; once in the log is enough.
            if line not in self._announced:
                self._announced.add(line)
                log.info("DRY-RUN would call %s", line)
                self.actions.append({"time": time.time(), "dry_run": True, "text": line})
            return None
        log.info("Calling %s", line)
        result = self._request(method, path, body)
        self.actions.append({"time": time.time(), "dry_run": False, "text": line})
        return result

    def info(self) -> dict:
        return self._read("/info")

    def spool_url(self, spool_id: int) -> str:
        return f"{self.url}/spool/show/{spool_id}"

    def extra_field_exists(self, entity_type: str, key: str) -> bool:
        return any(f.get("key") == key for f in self._read(f"/field/{entity_type}"))

    def create_extra_field(self, entity_type: str, key: str, name: str) -> None:
        self._write("POST", f"/field/{entity_type}/{key}", {"name": name, "field_type": "text"})

    def list_spools(self) -> list[dict]:
        return self._read("/spool?allow_archived=true")

    def get_spool(self, spool_id: int) -> dict:
        return self._read(f"/spool/{spool_id}")

    def list_vendors(self) -> list[dict]:
        return self._read("/vendor")

    def list_filaments(self) -> list[dict]:
        return self._read("/filament")

    def create_vendor(self, body: dict) -> dict | None:
        return self._write("POST", "/vendor", body)

    def create_filament(self, body: dict) -> dict | None:
        return self._write("POST", "/filament", body)

    def create_spool(self, body: dict) -> dict | None:
        return self._write("POST", "/spool", body)

    def update_spool(self, spool_id: int, body: dict) -> None:
        self._write("PATCH", f"/spool/{spool_id}", body)

    def update_filament(self, filament_id: int, body: dict) -> None:
        self._write("PATCH", f"/filament/{filament_id}", body)

    def use_spool(self, spool_id: int, weight: float) -> None:
        self._write("PUT", f"/spool/{spool_id}/use", {"use_weight": weight})
