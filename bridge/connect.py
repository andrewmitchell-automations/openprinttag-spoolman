"""Prusa Connect access: login tokens and the read-only printer queries.

This uses the same GraphQL API and OAuth refresh flow as the Connect web app.
Prusa does not document it, so it can change without notice.
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)

USER_AGENT = "openprinttag-spoolman/0.2"
REFRESH_MARGIN_SECONDS = 300

ME_QUERY = """
query BridgeMe {
  authorization { me { email organizations { id name } } }
}
"""

PRINTERS_QUERY = """
query BridgePrinters($organizationId: UUID!) {
  printer {
    printers(organizationId: $organizationId, first: 100, sortBy: NAME) {
      __typename
      ... on PrinterConnection { edges { node { id name state } } }
      ... on ConnectError { message }
    }
  }
}
"""

PRINTER_QUERY = """
query BridgePrinter($organizationId: UUID!, $printerId: UUID!) {
  printer {
    printer(organizationId: $organizationId, printerId: $printerId) {
      __typename
      ... on Printer {
        id
        name
        state
        fff {
          tools {
            id
            materialPackageInstance {
              __typename
              ... on FFFToolMaterialPackageInstance {
                uuid
                actualNettoFullWeight
                isFromNfc
                dynamicData { consumedWeight }
                package {
                  nominalNettoFullWeight
                  filamentDiameter
                  container { emptyWeight }
                  material {
                    uuid
                    name
                    abbreviation
                    brand { name }
                    primaryColor { colorRgba }
                    type { abbreviation }
                    properties { density }
                  }
                }
              }
              ... on ToolMaterialPackageInstanceEmpty { reason }
            }
          }
        }
      }
      ... on ConnectError { message }
    }
  }
}
"""

# A spool from the Warehouse in the Prusa app has little data on the tool, but its full data is here.
WAREHOUSE_QUERY = """
query BridgeWarehouse($organizationId: String!, $first: Int, $after: String) {
  warehouse {
    items(organizationId: $organizationId, first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        consumedWeight
        nominalNettoFullWeight
        materialPackageInstance {
          uuid
          actualNettoFullWeight
          package {
            filamentDiameter
            container { emptyWeight }
            material {
              uuid
              type
              primaryColor { colorRgba }
              properties { density }
              brand { name }
            }
          }
        }
      }
    }
  }
}
"""
WAREHOUSE_PAGE_SIZE = 100
WAREHOUSE_MAX_PAGES = 10


class ConnectAuthError(Exception):
    """The login has failed and needs to be done again."""


class ConnectError(Exception):
    """Connect returned an error or an unexpected response."""


def jwt_expiry(token: str) -> float | None:
    """Read the exp claim. The signature is not checked; Connect does that."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(padded))["exp"]
        return float(exp)
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def clean_token(raw: str) -> str:
    return raw.strip().strip("'\"")


def parse_login_json(raw: str) -> tuple[str, str] | None:
    """Reads {"access": ..., "refresh": ...}. Returns None for anything else."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("access"), str) or not data["access"]:
        return None
    refresh = data.get("refresh")
    return clean_token(data["access"]), clean_token(refresh) if isinstance(refresh, str) else ""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str = ""


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Tokens:
        try:
            data = json.loads(self.path.read_text())
            return Tokens(data["access_token"], data.get("refresh_token") or "")
        except FileNotFoundError as e:
            raise ConnectAuthError(f"{self.path} is missing; log in to Connect first") from e
        except (KeyError, ValueError) as e:
            raise ConnectAuthError(f"{self.path} is unreadable; log in to Connect again") from e

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class ConnectClient:
    def __init__(self, settings: Settings, store: TokenStore, http: httpx.Client | None = None):
        self.settings = settings
        self.store = store
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})

    def refresh(self) -> Tokens:
        tokens = self.store.load()
        if not tokens.refresh_token:
            raise ConnectAuthError("No refresh token is stored; log in to Connect again")
        try:
            response = self.http.post(
                f"{self.settings.account_url}/o/token/",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": self.settings.account_client_id,
                },
            )
        except httpx.HTTPError as e:
            raise ConnectError(f"Token refresh request failed: {type(e).__name__}") from e
        if response.status_code >= 500:
            raise ConnectError(f"Token refresh failed with HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("access_token"):
            reason = body.get("error") or "unknown error"
            raise ConnectAuthError(
                f"Token refresh rejected (HTTP {response.status_code}, {reason}); log in to Connect again"
            )
        # The server may rotate the refresh token, so store the new one before using it.
        new = Tokens(body["access_token"], body.get("refresh_token") or tokens.refresh_token)
        self.store.save(new)
        expiry = jwt_expiry(new.access_token)
        log.info("Refreshed the Connect token; expires in %s s", int(expiry - time.time()) if expiry else "?")
        return new

    def _valid_access_token(self) -> str:
        tokens = self.store.load()
        expiry = jwt_expiry(tokens.access_token)
        if expiry is None or expiry - time.time() > REFRESH_MARGIN_SECONDS:
            return tokens.access_token
        if tokens.refresh_token:
            return self.refresh().access_token
        if expiry > time.time():
            log.warning("The Connect token expires in %d s and no refresh token is stored", expiry - time.time())
            return tokens.access_token
        raise ConnectAuthError("The Connect token has expired and no refresh token is stored; log in to Connect again")

    def _post_query(self, token: str, query: str, variables: dict) -> httpx.Response:
        try:
            return self.http.post(
                self.settings.graphql_url,
                headers={"Authorization": f"Bearer {token}", "graphql-client-name": "connect-web"},
                json={"query": query, "variables": variables},
            )
        except httpx.HTTPError as e:
            raise ConnectError(f"GraphQL request failed: {type(e).__name__}") from e

    @staticmethod
    def _is_unauthenticated(response: httpx.Response) -> bool:
        if response.status_code == 401:
            return True
        try:
            errors = response.json().get("errors") or []
        except ValueError:
            return False
        return any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for e in errors)

    def _query(self, query: str, variables: dict | None = None) -> dict:
        variables = variables or {}
        response = self._post_query(self._valid_access_token(), query, variables)
        if self._is_unauthenticated(response):
            log.warning("Connect rejected the token; refreshing it once")
            response = self._post_query(self.refresh().access_token, query, variables)
            if self._is_unauthenticated(response):
                raise ConnectAuthError("Connect rejected a freshly refreshed token; log in to Connect again")
        if response.status_code != 200:
            raise ConnectError(f"GraphQL request returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as e:
            raise ConnectError("GraphQL response was not JSON") from e
        if body.get("errors"):
            messages = "; ".join(str(e.get("message")) for e in body["errors"])
            raise ConnectError(f"GraphQL errors: {messages}")
        if not isinstance(body.get("data"), dict):
            raise ConnectError("GraphQL response has no data")
        return body["data"]

    def me(self) -> dict:
        try:
            return self._query(ME_QUERY)["authorization"]["me"]
        except (KeyError, TypeError) as e:
            raise ConnectError("GraphQL response has an unexpected shape") from e

    def list_printers(self) -> list[dict]:
        """Every printer in every organization the login can see."""
        printers = []
        for org in self.me().get("organizations") or []:
            data = self._query(PRINTERS_QUERY, {"organizationId": org["id"]})
            result = (data.get("printer") or {}).get("printers") or {}
            if result.get("__typename") != "PrinterConnection":
                log.warning("Could not list printers in %s: %s", org.get("name"), result.get("message"))
                continue
            for edge in result.get("edges") or []:
                node = edge["node"]
                printers.append(
                    {
                        "uuid": node["id"],
                        "name": node.get("name") or node["id"],
                        "state": node.get("state"),
                        "organization_id": org["id"],
                        "organization_name": org.get("name") or "",
                    }
                )
        return printers

    def fetch_printer(self, organization_id: str, printer_uuid: str) -> dict:
        data = self._query(PRINTER_QUERY, {"organizationId": organization_id, "printerId": printer_uuid})
        try:
            printer = data["printer"]["printer"]
        except (KeyError, TypeError) as e:
            raise ConnectError("GraphQL response has an unexpected shape") from e
        if not printer or printer.get("__typename") != "Printer":
            detail = (printer or {}).get("message") or (printer or {}).get("__typename")
            raise ConnectError(f"Printer query failed: {detail}")
        return printer

    def warehouse_items(self, organization_id: str) -> list[dict]:
        items = []
        after = None
        for _ in range(WAREHOUSE_MAX_PAGES):
            data = self._query(WAREHOUSE_QUERY, {"organizationId": organization_id, "first": WAREHOUSE_PAGE_SIZE, "after": after})
            page = (data.get("warehouse") or {}).get("items") or {}
            items.extend(page.get("nodes") or [])
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        return items
