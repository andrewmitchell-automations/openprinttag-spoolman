"""One poll: read a printer's loaded spools from Connect and bring Spoolman up to date."""

import json
import logging
import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .config import PrinterConfig
from .spoolman import SpoolmanClient, SpoolmanError, encode_extra, extra_value

log = logging.getLogger(__name__)

SPOOL_UUID_FIELD = "opt_instance_uuid"
FILAMENT_UUID_FIELD = "opt_material_uuid"
WEIGHT_EPSILON = 0.001
HIGH_CONFIDENCE = 70
SUGGESTION_THRESHOLD = 45


@dataclass(frozen=True)
class LoadedSpool:
    tool_id: str
    uuid: str
    is_from_nfc: bool
    actual_weight: float | None
    consumed_weight: float | None
    nominal_weight: float | None
    empty_weight: float | None
    filament_diameter: float | None
    material_uuid: str | None
    material_name: str | None
    material_type: str | None
    brand: str | None
    color_hex: str | None
    density: float | None

    @property
    def remaining_fraction(self) -> float:
        full = self.nominal_weight or self.actual_weight or 0
        if not full or self.actual_weight is None:
            return 0.0
        return max(0.0, min(1.0, (self.actual_weight - (self.consumed_weight or 0)) / full))


def color_hex(rgba: str | None) -> str | None:
    """'#212929' or '#212929FF' becomes '212929'."""
    if not rgba:
        return None
    value = rgba.lstrip("#")
    if len(value) not in (6, 8):
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value[:6].upper()


def parse_loaded_spools(printer: dict) -> list[LoadedSpool]:
    spools = []
    for tool in (printer.get("fff") or {}).get("tools") or []:
        instance = tool.get("materialPackageInstance") or {}
        if instance.get("__typename") != "FFFToolMaterialPackageInstance" or not instance.get("uuid"):
            log.debug("Tool %s has no spool loaded (%s)", tool.get("id"), instance.get("reason") or "no data")
            continue
        package = instance.get("package") or {}
        material = package.get("material") or {}
        spools.append(
            LoadedSpool(
                tool_id=str(tool.get("id")),
                uuid=instance["uuid"],
                is_from_nfc=bool(instance.get("isFromNfc")),
                actual_weight=instance.get("actualNettoFullWeight"),
                consumed_weight=(instance.get("dynamicData") or {}).get("consumedWeight"),
                nominal_weight=package.get("nominalNettoFullWeight"),
                empty_weight=(package.get("container") or {}).get("emptyWeight"),
                filament_diameter=package.get("filamentDiameter"),
                material_uuid=material.get("uuid"),
                material_name=material.get("name"),
                material_type=(material.get("type") or {}).get("abbreviation") or material.get("abbreviation"),
                brand=(material.get("brand") or {}).get("name"),
                color_hex=color_hex((material.get("primaryColor") or {}).get("colorRgba")),
                density=(material.get("properties") or {}).get("density"),
            )
        )
    return spools


def consumption_step(previous: float | None, current: float) -> tuple[str, float]:
    """Returns (action, weight to deduct), where action is baseline, none, use or rebase."""
    if previous is None:
        return "baseline", 0.0
    delta = current - previous
    if delta > WEIGHT_EPSILON:
        return "use", delta
    if delta < -WEIGHT_EPSILON:
        return "rebase", 0.0
    return "none", 0.0


def index_spools(spools: list[dict]) -> dict[str, dict]:
    index = {}
    for spool in spools:
        uuid = extra_value(spool, SPOOL_UUID_FIELD)
        if not uuid:
            continue
        if uuid in index:
            log.warning("Spools %s and %s share instance UUID %s; using the first", index[uuid]["id"], spool["id"], uuid)
            continue
        index[uuid] = spool
    return index


def _color_distance(a: str | None, b: str | None) -> float | None:
    if not a or not b or len(a) != 6 or len(b) != 6:
        return None
    try:
        return sum(abs(int(a[i : i + 2], 16) - int(b[i : i + 2], 16)) for i in (0, 2, 4)) / (3 * 255)
    except ValueError:
        return None


def name_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    tokens = [" ".join(sorted(name.casefold().split())) for name in (a, b)]
    return SequenceMatcher(None, *tokens).ratio()


def score_spool(loaded: LoadedSpool, spool: dict, other_locations: tuple[str, ...] = ()) -> dict:
    """How likely it is that a Spoolman spool is this physical spool, with the reasons."""
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or ""
    material = filament.get("material") or ""
    color = (filament.get("color_hex") or "").upper()[:6]
    score = 0
    reasons = []
    if loaded.material_uuid and extra_value(filament, FILAMENT_UUID_FIELD) == loaded.material_uuid:
        score += 40
        reasons.append("Same OpenPrintTag material")
    if loaded.brand and vendor.casefold() == loaded.brand.casefold():
        score += 25
        reasons.append("Same brand")
    if loaded.material_type and material.casefold() == loaded.material_type.casefold():
        score += 15
        reasons.append("Same material type")
    similarity = name_similarity(loaded.material_name, filament.get("name"))
    if similarity >= 0.6:
        score += round(25 * similarity)
        reasons.append(f"Name {round(similarity * 100)}% similar")
    distance = _color_distance(loaded.color_hex, color)
    if distance is not None and distance < 0.05:
        score += 10
        reasons.append(f"Color very close (#{color} and #{loaded.color_hex})")
    elif distance is not None and distance < 0.15:
        score += 5
        reasons.append(f"Color close (#{color} and #{loaded.color_hex})")
    if loaded.nominal_weight and filament.get("weight") == loaded.nominal_weight:
        score += 5
        reasons.append("Same nominal weight")
    remaining = spool.get("remaining_weight")
    if remaining is not None and loaded.actual_weight is not None:
        if abs(remaining - (loaded.actual_weight - (loaded.consumed_weight or 0))) <= 50:
            score += 5
            reasons.append("Remaining weight is close")
    if spool.get("location") and spool["location"] in other_locations:
        score -= 15
        reasons.append("Located at another printer")
    if score >= HIGH_CONFIDENCE:
        confidence = "High"
    elif score >= SUGGESTION_THRESHOLD:
        confidence = "Medium"
    else:
        confidence = "Low"
    return {
        "id": spool["id"],
        "label": f"#{spool['id']} {vendor} {filament.get('name') or ''} ({material}, {round(remaining or 0)} g)".replace("  ", " "),
        "score": score,
        "confidence": confidence,
        "reasons": reasons,
        "suggested": score >= SUGGESTION_THRESHOLD,
    }


def rank_spools_for_link(loaded: LoadedSpool, spools: list[dict], other_locations: tuple[str, ...] = ()) -> list[dict]:
    """Candidates for an unlinked spool, best first. Nothing is linked until the user confirms."""
    ranked = [
        score_spool(loaded, spool, other_locations)
        for spool in spools
        if not spool.get("archived") and not extra_value(spool, SPOOL_UUID_FIELD)
    ]
    ranked.sort(key=lambda c: (-c["score"], c["id"]))
    for position, candidate in enumerate(ranked):
        candidate["best"] = position == 0 and candidate["suggested"]
    return ranked


def _differs(before, after) -> bool:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return abs(before - after) > WEIGHT_EPSILON
    return before != after


def build_link_changes(loaded: LoadedSpool, spool: dict, shared: int) -> list[dict]:
    """The rows on the review page. Filament changes are off if other spools share the filament."""
    filament = spool.get("filament") or {}
    filament_on = shared == 0
    rows = [
        ("spool_instance", "spool", SPOOL_UUID_FIELD, "Link to this tag", extra_value(spool, SPOOL_UUID_FIELD), loaded.uuid, True),
        ("spool_initial_weight", "spool", "initial_weight", "Spool: filament weight", spool.get("initial_weight"), loaded.actual_weight, True),
        ("spool_spool_weight", "spool", "spool_weight", "Spool: empty weight", spool.get("spool_weight"), loaded.empty_weight, True),
        ("spool_used_weight", "spool", "used_weight", "Spool: consumed", spool.get("used_weight"), loaded.consumed_weight, True),
        ("filament_name", "filament", "name", "Filament: name", filament.get("name"), loaded.material_name, filament_on),
        ("filament_color_hex", "filament", "color_hex", "Filament: color", (filament.get("color_hex") or "").upper() or None, loaded.color_hex, filament_on),
        ("filament_weight", "filament", "weight", "Filament: nominal weight", filament.get("weight"), loaded.nominal_weight, filament_on),
        ("filament_spool_weight", "filament", "spool_weight", "Filament: empty weight", filament.get("spool_weight"), loaded.empty_weight, filament_on),
        ("filament_material", "filament", FILAMENT_UUID_FIELD, "Filament: OpenPrintTag material", extra_value(filament, FILAMENT_UUID_FIELD), loaded.material_uuid, filament_on),
    ]
    changes = []
    for key, target, field, label, before, after, checked in rows:
        if after is None or not _differs(before, after):
            continue
        changes.append(
            {"key": key, "target": target, "field": field, "label": label, "before": before, "after": after, "checked": checked, "locked": key == "spool_instance"}
        )
    return changes


class State:
    """Last seen consumed weight per instance UUID.

    A dry run keeps only its first baseline on disk, under dry_baseline. Going live
    continues from there, so consumption during the dry run is still deducted.
    """

    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        self.consumed: dict[str, float] = {}
        self.dry_baseline: dict[str, float] = {}
        self._seen: dict[str, float] = {}
        try:
            data = json.loads(path.read_text())
            self.consumed = data.get("consumed", {})
            self.dry_baseline = data.get("dry_baseline", {})
        except FileNotFoundError:
            pass
        except ValueError:
            log.error("%s is unreadable; starting from new baselines", path)

    def previous(self, uuid: str) -> float | None:
        if self.dry_run and uuid in self._seen:
            return self._seen[uuid]
        return self.consumed.get(uuid, self.dry_baseline.get(uuid))

    def record(self, uuid: str, consumed: float, action: str = "use") -> None:
        if self.dry_run:
            self._seen[uuid] = consumed
            if action in ("baseline", "rebase") and uuid not in self.consumed:
                self.dry_baseline[uuid] = consumed
                self._save()
            return
        self.consumed[uuid] = consumed
        self.dry_baseline.pop(uuid, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"consumed": self.consumed, "dry_baseline": self.dry_baseline}, indent=2))
        os.replace(tmp, self.path)


class Bridge:
    def __init__(self, connect, spoolman: SpoolmanClient, state: State, auto_create: bool = False):
        self.connect = connect
        self.spoolman = spoolman
        self.state = state
        self.auto_create = auto_create
        self.fields_ready = False
        self._warned_unlinked: set[str] = set()

    def poll(self, printer_config: PrinterConfig) -> dict:
        printer = self.connect.fetch_printer(printer_config.organization_id, printer_config.uuid)
        loaded = parse_loaded_spools(printer)
        log.info("Printer %s is %s with %d spool(s) loaded", printer.get("name"), printer.get("state"), len(loaded))
        self._ensure_extra_fields()
        spools = index_spools(self.spoolman.list_spools())
        tools = []
        for spool in loaded:
            matched = spools.get(spool.uuid)
            spool_id = self._sync_spool(spool, matched, printer_config.location)
            tools.append({"spool": spool, "spool_id": spool_id})
        return {"name": printer.get("name"), "state": printer.get("state"), "tools": tools, "time": time.time()}

    def _ensure_extra_fields(self) -> None:
        if self.fields_ready:
            return
        if not self.spoolman.extra_field_exists("spool", SPOOL_UUID_FIELD):
            self.spoolman.create_extra_field("spool", SPOOL_UUID_FIELD, "OpenPrintTag instance UUID")
        if not self.spoolman.extra_field_exists("filament", FILAMENT_UUID_FIELD):
            self.spoolman.create_extra_field("filament", FILAMENT_UUID_FIELD, "OpenPrintTag material UUID")
        self.fields_ready = not self.spoolman.dry_run

    def _sync_spool(self, loaded: LoadedSpool, spool: dict | None, location: str) -> int | None:
        log.info(
            "Tool %s: %s %s, instance %s, actual %s g, consumed %s g, NFC %s",
            loaded.tool_id, loaded.brand, loaded.material_name, loaded.uuid,
            loaded.actual_weight, loaded.consumed_weight, loaded.is_from_nfc,
        )
        if not loaded.is_from_nfc:
            log.warning("Skipping instance %s because it was not read from an NFC tag", loaded.uuid)
            return None
        if spool is None:
            if self.auto_create:
                created = self.create_spool(loaded, location)
                return created["id"] if created else None
            if loaded.uuid not in self._warned_unlinked:
                self._warned_unlinked.add(loaded.uuid)
                log.warning("Instance %s is not linked to a Spoolman spool; waiting for a choice", loaded.uuid)
            return None
        log.info("Instance %s is Spoolman spool %s", loaded.uuid, spool["id"])
        if spool.get("archived"):
            log.warning("Spool %s is archived in Spoolman but loaded in the printer", spool["id"])
        if location and spool.get("location") != location:
            self.spoolman.update_spool(spool["id"], {"location": location})
        self._sync_consumption(loaded, spool["id"])
        return spool["id"]

    def _sync_consumption(self, loaded: LoadedSpool, spool_id: int) -> None:
        if loaded.consumed_weight is None:
            log.warning("Connect reported no consumed weight for instance %s", loaded.uuid)
            return
        current = float(loaded.consumed_weight)
        previous = self.state.previous(loaded.uuid)
        action, delta = consumption_step(previous, current)
        if action == "baseline":
            log.info("First sighting of instance %s; baseline %s g, nothing deducted", loaded.uuid, current)
        elif action == "rebase":
            log.warning("Consumed weight for instance %s fell from %s g to %s g; rebasing", loaded.uuid, previous, current)
        elif action == "use":
            log.info("Instance %s consumed %s g more", loaded.uuid, round(delta, 3))
            self.spoolman.use_spool(spool_id, delta)
        else:
            return
        self.state.record(loaded.uuid, current, action)

    def link_spool(self, loaded: LoadedSpool, spool_id: int, selected: set[str]) -> None:
        """Apply the review page's choices. Other extra fields, like card_uids, are kept."""
        self._ensure_extra_fields()
        spool = self.spoolman.get_spool(spool_id)
        taken = extra_value(spool, SPOOL_UUID_FIELD)
        if taken and taken != loaded.uuid:
            raise SpoolmanError(f"Spool {spool_id} is already linked to instance {taken}")
        changes = {c["key"]: c for c in build_link_changes(loaded, spool, 0)}
        spool_extra = dict(spool.get("extra") or {})
        spool_extra[SPOOL_UUID_FIELD] = encode_extra(loaded.uuid)
        spool_body: dict = {"extra": spool_extra}
        filament_body: dict = {}
        filament = spool.get("filament") or {}
        for key in selected:
            change = changes.get(key)
            if not change or change["locked"]:
                continue
            if change["field"] == FILAMENT_UUID_FIELD:
                extra = dict(filament.get("extra") or {})
                extra[FILAMENT_UUID_FIELD] = encode_extra(change["after"])
                filament_body["extra"] = extra
            elif change["target"] == "spool":
                spool_body[change["field"]] = change["after"]
            else:
                filament_body[change["field"]] = change["after"]
        if filament_body:
            self.spoolman.update_filament(filament["id"], dict(sorted(filament_body.items())))
        self.spoolman.update_spool(spool_id, dict(sorted(spool_body.items())))
        # The spool now accounts for everything up to this point, or starts counting from it.
        if loaded.consumed_weight is not None and not self.spoolman.dry_run:
            self.state.record(loaded.uuid, float(loaded.consumed_weight), "baseline")

    def unlink_spool(self, spool_id: int) -> None:
        spool = self.spoolman.get_spool(spool_id)
        extra = {k: v for k, v in (spool.get("extra") or {}).items() if k != SPOOL_UUID_FIELD}
        self.spoolman.update_spool(spool_id, {"extra": extra})
        if not self.spoolman.dry_run and extra_value(self.spoolman.get_spool(spool_id), SPOOL_UUID_FIELD):
            raise SpoolmanError(f"Spoolman kept the link on spool {spool_id}")

    def create_preview(self, loaded: LoadedSpool) -> dict:
        vendor_id = next((v["id"] for v in self.spoolman.list_vendors() if v["name"].casefold() == (loaded.brand or "").casefold()), None)
        filament_exists = vendor_id is not None and any(
            (f.get("vendor") or {}).get("id") == vendor_id and (f.get("name") or "").casefold() == (loaded.material_name or "").casefold()
            for f in self.spoolman.list_filaments()
        )
        return {"vendor_exists": vendor_id is not None, "filament_exists": filament_exists}

    def create_spool(self, loaded: LoadedSpool, location: str) -> dict | None:
        self._ensure_extra_fields()
        problems = [
            name
            for name, value in (
                ("brand", loaded.brand),
                ("material name", loaded.material_name),
                ("density", loaded.density),
                ("filament diameter", loaded.filament_diameter),
            )
            if value is None
        ]
        if problems:
            raise SpoolmanError(f"Cannot create a spool for instance {loaded.uuid}; Connect did not report: {', '.join(problems)}")
        filament_id = self._find_or_create_filament(loaded)
        consumed = float(loaded.consumed_weight or 0)
        created = self.spoolman.create_spool(
            {
                "filament_id": filament_id,
                "initial_weight": loaded.actual_weight,
                "spool_weight": loaded.empty_weight,
                # Filament used before the spool reached Spoolman, so remaining weight is right.
                "used_weight": consumed,
                "location": location,
                "extra": {SPOOL_UUID_FIELD: encode_extra(loaded.uuid)},
            }
        )
        if created is not None:
            log.info("Created Spoolman spool %s for instance %s", created["id"], loaded.uuid)
        if loaded.consumed_weight is not None:
            self.state.record(loaded.uuid, consumed, "baseline")
        return created

    def _find_or_create_vendor(self, loaded: LoadedSpool) -> int | str:
        for vendor in self.spoolman.list_vendors():
            if vendor["name"].casefold() == loaded.brand.casefold():
                return vendor["id"]
        created = self.spoolman.create_vendor({"name": loaded.brand})
        return created["id"] if created else "<id of new vendor>"

    def _find_or_create_filament(self, loaded: LoadedSpool) -> int | str:
        vendor_id = self._find_or_create_vendor(loaded)
        for filament in self.spoolman.list_filaments():
            if (
                (filament.get("vendor") or {}).get("id") == vendor_id
                and (filament.get("name") or "").casefold() == loaded.material_name.casefold()
            ):
                log.info("Using existing Spoolman filament %s", filament["id"])
                return filament["id"]
        body = {
            "vendor_id": vendor_id,
            "name": loaded.material_name,
            "material": loaded.material_type,
            "density": loaded.density,
            "diameter": loaded.filament_diameter,
            "weight": loaded.nominal_weight,
            "spool_weight": loaded.empty_weight,
            "color_hex": loaded.color_hex,
        }
        if loaded.material_uuid:
            body["extra"] = {FILAMENT_UUID_FIELD: encode_extra(loaded.material_uuid)}
        created = self.spoolman.create_filament(body)
        return created["id"] if created else "<id of new filament>"
