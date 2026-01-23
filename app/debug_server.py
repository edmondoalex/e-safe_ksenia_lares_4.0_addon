import os
import re
from pathlib import Path
import json
import logging
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UI_REV = "2026-01-12.07"
# Keep a code-side version so the UI shows the right value even when
# Supervisor doesn't inject / update ADDON_VERSION (common when config.yaml isn't bundled in the container image).
CODE_VERSION = ""

_UI_LOGGER = logging.getLogger("ksenia_lares_addon.ui")
def _read_addon_version_from_config() -> str:
    # Prefer config.yaml when running from a dev checkout, so the UI version matches the repo.
    try:
        here = Path(__file__).resolve()
        candidates = [
            here.parent.parent / "config.yaml",  # repo checkout layout
            here.parent / "config.yaml",         # container layout (app/ copied to /app)
            Path("/app/config.yaml"),            # explicit fallback
            Path("/config.yaml"),
        ]
        for cfg in candidates:
            try:
                if not cfg.exists():
                    continue
                txt = cfg.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'(?m)^\s*version\s*:\s*"?([^"\r\n]+)"?\s*$', txt)
                ver = (m.group(1).strip() if m else "") or ""
                if ver:
                    return ver
            except Exception:
                continue
        return ""
    except Exception:
        return ""


def _detect_addon_version() -> str:
    # Supervisor typically injects ADDON_VERSION, but in local dev it can lag behind.
    env_ver = str(os.getenv("ADDON_VERSION", "") or "").strip()
    file_ver = _read_addon_version_from_config()
    return env_ver or file_ver or CODE_VERSION


ADDON_VERSION = _detect_addon_version()
UI_VERSION = ADDON_VERSION
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "www")
_ASSET_MAP = {
    "alarm": "e-safe alarm.png",
    "arm": "e-safe arm.png",
    "disarm": "e-safe disarm.png",
    "partial": "e-safe partial.png",
    "logo_ekonex": "logo_ekonex.png",
    "e-safe_scr": "e-safe_scr.png",
}
_UI_TAGS_PATH = "/data/ui_tags.json"
_UI_THERM_NAMES_PATH = "/data/ui_thermostat_names.json"
_UI_THERM_NAMES_CACHE = {"ts": 0.0, "data": {}}
_UI_FAVORITES_PATH = "/data/ui_favorites.json"
_UI_FAVORITES_LOCK = threading.Lock()
_ZONES_LAST_SEEN_PATH = "/data/last_seen_zones.json"
_ZONES_LAST_SEEN_FLUSH_SEC = 5.0


class LaresState:
    def __init__(self):
        self._lock = threading.Lock()
        self._entities = {}  # key: (entity_type, id) -> entity dict
        self._meta = {"started_at": time.time(), "last_update": None, "ws1_connected": False}
        self._subs_lock = threading.Lock()
        self._subs = set()
        self._zones_last_seen = self._load_zones_last_seen()
        self._zones_last_seen_dirty = False
        self._zones_last_seen_last_flush = 0.0

    def set_meta(self, key: str, value):
        try:
            if not key:
                return
            with self._lock:
                self._meta[str(key)] = value
        except Exception:
            pass

    def _load_zones_last_seen(self):
        try:
            with open(_ZONES_LAST_SEEN_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return {}
            out = {}
            for k, v in raw.items():
                try:
                    ks = str(k)
                    ts = float(v)
                    if ts > 0:
                        out[ks] = ts
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _maybe_flush_zones_last_seen(self, now: float):
        try:
            if not self._zones_last_seen_dirty:
                return
            if (now - float(self._zones_last_seen_last_flush or 0.0)) < float(
                _ZONES_LAST_SEEN_FLUSH_SEC
            ):
                return
            tmp = _ZONES_LAST_SEEN_PATH + ".tmp"
            os.makedirs(os.path.dirname(_ZONES_LAST_SEEN_PATH), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._zones_last_seen, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _ZONES_LAST_SEEN_PATH)
            self._zones_last_seen_last_flush = now
            self._zones_last_seen_dirty = False
        except Exception:
            # Keep dirty flag so we retry on next update.
            self._zones_last_seen_dirty = True

    def get_realtime(self, entity_type, entity_id):
        if entity_id is None:
            return None

        # Entity IDs sometimes arrive as int in realtime updates and as str elsewhere;
        # try a few normalized keys to avoid publishing partial JSON to MQTT.
        candidates = []
        candidates.append(entity_id)
        try:
            sid = str(entity_id)
            candidates.append(sid)
            if sid.isdigit():
                candidates.append(int(sid))
        except Exception:
            pass
        try:
            if isinstance(entity_id, int):
                candidates.append(str(entity_id))
        except Exception:
            pass

        with self._lock:
            for cand in candidates:
                key = f"{entity_type}:{cand}"
                current = self._entities.get(key)
                if not isinstance(current, dict):
                    continue
                rt = current.get("realtime")
                if not isinstance(rt, dict) or not rt:
                    continue
                return dict(rt)
        return None

    def get_merged(self, entity_type, entity_id):
        if entity_id is None:
            return None

        # Try same normalization strategy as get_realtime().
        candidates = []
        candidates.append(entity_id)
        try:
            sid = str(entity_id)
            candidates.append(sid)
            if sid.isdigit():
                candidates.append(int(sid))
        except Exception:
            pass
        try:
            if isinstance(entity_id, int):
                candidates.append(str(entity_id))
        except Exception:
            pass

        with self._lock:
            for cand in candidates:
                key = f"{entity_type}:{cand}"
                current = self._entities.get(key)
                if not isinstance(current, dict):
                    continue
                st = current.get("static") if isinstance(current.get("static"), dict) else {}
                rt = current.get("realtime") if isinstance(current.get("realtime"), dict) else {}
                merged = {}
                st = st or {}
                rt = rt or {}
                merged.update(st)

                # Smart merge for thermostats: realtime payloads can be partial and sometimes include
                # empty/placeholder values. Do not let them clobber the last known config fields.
                if str(entity_type).lower() == "thermostats":
                    def _is_empty(v):
                        if v is None:
                            return True
                        if isinstance(v, str) and v.strip() == "":
                            return True
                        return False

                    for k, v in rt.items():
                        if k in ("ACT_MODE", "ACT_SEA", "MAN_HRS") and _is_empty(v) and k in st:
                            continue
                        if k in ("WIN", "SUM", "TOF") and isinstance(v, dict):
                            cur = merged.get(k)
                            if not isinstance(cur, dict):
                                cur = {}
                            # Merge only meaningful fields from realtime.
                            patch = {kk: vv for kk, vv in v.items() if not _is_empty(vv)}
                            merged[k] = {**cur, **patch}
                            continue
                        merged[k] = v
                else:
                    merged.update(rt)
                if not merged:
                    continue
                if "ID" not in merged:
                    merged["ID"] = cand
                return merged
        return None

    def snapshot(self):
        with self._lock:
            entities = list(self._entities.values())
            meta = dict(self._meta)
        # Apply UI-level overrides (persisted in /data) such as thermostat custom names.
        try:
            now = time.time()
            if (now - float(_UI_THERM_NAMES_CACHE.get("ts") or 0.0)) > 3.0:
                _UI_THERM_NAMES_CACHE["data"] = _load_ui_thermostat_names()
                _UI_THERM_NAMES_CACHE["ts"] = now
            names = _UI_THERM_NAMES_CACHE.get("data") or {}
            if isinstance(names, dict) and names:
                for e in entities:
                    if str(e.get("type") or "").lower() != "thermostats":
                        continue
                    tid = str(e.get("id") or "")
                    des = names.get(tid)
                    if isinstance(des, str) and des.strip():
                        e["name"] = des.strip()
                        st = e.get("static")
                        if isinstance(st, dict):
                            st["DES"] = des.strip()
        except Exception:
            pass
        return {"meta": meta, "entities": entities}

    def set_ws1_status(self, connected: bool):
        now = time.time()
        with self._lock:
            prev = bool(self._meta.get("ws1_connected", False))
            self._meta["ws1_connected"] = bool(connected)
            self._meta["ws1_last_change"] = now
            # Don't bump last_update; it's for data updates.
        if prev != bool(connected):
            self._publish_event({"type": "update", "meta": {"ws1_connected": bool(connected)}, "entities": []})

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._subs_lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._subs_lock:
            self._subs.discard(q)

    def _publish_event(self, event: dict):
        if not event:
            return
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def set_initial_data(self, read_data, realtime_initial):
        now = time.time()
        changed = []
        with self._lock:
            if read_data:
                changed.extend(self._ingest_read_data(read_data, now))
            if realtime_initial:
                changed.extend(
                    self._ingest_realtime_payload(realtime_initial.get("PAYLOAD", {}), now)
                )
            self._meta["last_update"] = now
        if changed:
            self._publish_event({"type": "update", "meta": {"last_update": now}, "entities": changed})

    def apply_realtime_update(self, entity_type, updates):
        now = time.time()
        changed = []
        with self._lock:
            if entity_type in ("lights", "covers", "switches") and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("outputs", item.get("ID"), {"realtime": item}, now))
            elif entity_type == "domus" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("domus", item.get("ID"), {"realtime": item}, now))
            elif entity_type == "powerlines" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("powerlines", item.get("ID"), {"realtime": item}, now))
            elif entity_type == "partitions" and isinstance(updates, list):
                for item in updates:
                    changed.append(
                        self._upsert("partitions", item.get("ID"), {"realtime": item}, now)
                    )
            elif entity_type == "zones" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("zones", item.get("ID"), {"realtime": item}, now))
            elif entity_type == "systems" and isinstance(updates, list):
                for item in updates:
                    changed.append(
                        self._upsert("systems", item.get("ID"), {"realtime": item}, now)
                    )
            elif entity_type == "connection" and isinstance(updates, list):
                for item in updates:
                    changed.append(
                        self._upsert("connection", item.get("ID"), {"realtime": item}, now)
                    )
                    gsm = _gsm_from_connection_item(item)
                    if gsm:
                        changed.append(self._upsert("gsm", gsm.get("ID"), {"realtime": gsm}, now))
            elif entity_type == "thermostats" and isinstance(updates, list):
                for item in updates:
                    changed.append(
                        self._upsert("thermostats", item.get("ID"), {"realtime": item}, now)
                    )
            elif entity_type == "gsm" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("gsm", item.get("ID"), {"realtime": item}, now))
            elif entity_type == "logs" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("logs", item.get("ID"), {"realtime": item}, now))
            elif entity_type == "schedulers" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("schedulers", item.get("ID"), {"realtime": item}, now))
            self._meta["last_update"] = now
        changed = [c for c in changed if c]
        if changed:
            self._publish_event({"type": "update", "meta": {"last_update": now}, "entities": changed})

    def apply_static_update(self, entity_type, updates):
        now = time.time()
        changed = []
        with self._lock:
            if entity_type in ("lights", "covers", "switches") and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("outputs", item.get("ID"), {"static": item}, now))
            elif entity_type == "domus" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("domus", item.get("ID"), {"static": item}, now))
            elif entity_type == "powerlines" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("powerlines", item.get("ID"), {"static": item}, now))
            elif entity_type == "partitions" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("partitions", item.get("ID"), {"static": item}, now))
            elif entity_type == "zones" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("zones", item.get("ID"), {"static": item}, now))
            elif entity_type == "systems" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("systems", item.get("ID"), {"static": item}, now))
            elif entity_type == "connection" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("connection", item.get("ID"), {"static": item}, now))
                    gsm = _gsm_from_connection_item(item)
                    if gsm:
                        changed.append(self._upsert("gsm", gsm.get("ID"), {"static": gsm}, now))
            elif entity_type == "gsm" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("gsm", item.get("ID"), {"static": item}, now))
            elif entity_type == "schedulers" and isinstance(updates, list):
                for item in updates:
                    changed.append(
                        self._upsert("schedulers", item.get("ID"), {"static": item}, now)
                    )
            elif entity_type == "thermostats" and isinstance(updates, list):
                for item in updates:
                    changed.append(
                        self._upsert("thermostats", item.get("ID"), {"static": item}, now)
                    )
            elif entity_type == "accounts" and isinstance(updates, list):
                for item in updates:
                    changed.append(self._upsert("accounts", item.get("ID"), {"static": item}, now))
            self._meta["last_update"] = now
        changed = [c for c in changed if c]
        if changed:
            self._publish_event({"type": "update", "meta": {"last_update": now}, "entities": changed})

    def _upsert(self, entity_type, entity_id, patch, now):
        if entity_id is None:
            return None

        def _norm_id(v):
            try:
                if v is None:
                    return None
                if isinstance(v, int):
                    return str(v)
                s = str(v).strip()
                if s.isdigit():
                    return str(int(s))
                return s
            except Exception:
                return str(v)

        norm_id = _norm_id(entity_id)
        key = f"{entity_type}:{norm_id}"
        current = self._entities.get(key)
        is_new = current is None
        if current is None:
            last_seen_seed = 0.0
            if str(entity_type).lower() == "zones":
                try:
                    last_seen_seed = float(self._zones_last_seen.get(str(norm_id)) or 0.0)
                except Exception:
                    last_seen_seed = 0.0
            current = {
                "key": key,
                "type": entity_type,
                "id": norm_id,
                "access": _infer_access(entity_type),
                "name": None,
                "static": {},
                "realtime": {},
                "last_seen": last_seen_seed,
                "_rt_initialized": False,
            }
        if "static" in patch:
            current["static"] = {**current.get("static", {}), **(patch["static"] or {})}
        if "realtime" in patch:
            prev_rt = current.get("realtime", {}) if isinstance(current.get("realtime"), dict) else {}
            next_rt = {**prev_rt, **(patch["realtime"] or {})}
            current["realtime"] = next_rt
        if not current.get("name"):
            current["name"] = _infer_name(current["static"]) or _infer_name(current["realtime"])
        if str(entity_type).lower() == "zones":
            # For zones, "last_seen" is the last *event* time (realtime update), not config/static refresh.
            if "realtime" in patch:
                def _n(v):
                    return str(v if v is not None else "").strip().upper()

                prev_rt = prev_rt if "prev_rt" in locals() else {}
                next_rt = current.get("realtime", {}) if isinstance(current.get("realtime"), dict) else {}
                rt_initialized = bool(current.get("_rt_initialized", False))
                if not rt_initialized:
                    current["_rt_initialized"] = True
                    # Avoid clobbering persisted ordering on startup: the first realtime snapshot
                    # often looks like a "change" because prev_rt is empty.
                    # Only bump last_seen if we have no history AND the zone is currently in a trouble state.
                    trouble_now = False
                    try:
                        sta = _n(next_rt.get("STA"))
                        byp = _n(next_rt.get("BYP"))
                        t = _n(next_rt.get("T"))
                        vas = _n(next_rt.get("VAS"))
                        fm = _n(next_rt.get("FM"))
                        a = _n(next_rt.get("A"))
                        trouble_now = (
                            (sta == "A")
                            or (a not in ("", "N", "0", "F", "OFF", "FALSE", "NO"))
                            or (byp not in ("", "NO", "N", "0", "OFF", "FALSE"))
                            or (t not in ("", "N", "0", "F", "OFF", "FALSE", "NO", "NA"))
                            or (vas not in ("", "F", "N", "0", "OFF", "FALSE", "NO", "NA"))
                            or (fm not in ("", "F", "N", "0", "OFF", "FALSE", "NO", "NA"))
                        )
                    except Exception:
                        trouble_now = False

                    if trouble_now and float(current.get("last_seen") or 0.0) <= 0.0:
                        current["last_seen"] = now
                        zid = str(norm_id)
                        prev = float(self._zones_last_seen.get(zid) or 0.0)
                        if float(now) > prev:
                            self._zones_last_seen[zid] = float(now)
                            self._zones_last_seen_dirty = True
                            self._maybe_flush_zones_last_seen(now)
                else:
                    event_changed = False
                    for k in ("STA", "BYP", "T", "VAS", "FM", "A"):
                        if _n(prev_rt.get(k)) != _n(next_rt.get(k)):
                            event_changed = True
                            break

                    if event_changed:
                        current["last_seen"] = now
                        zid = str(norm_id)
                        prev = float(self._zones_last_seen.get(zid) or 0.0)
                        if float(now) > prev:
                            self._zones_last_seen[zid] = float(now)
                            self._zones_last_seen_dirty = True
                            self._maybe_flush_zones_last_seen(now)
            elif is_new:
                # Keep seeded cached value.
                pass
        else:
            current["last_seen"] = now
        self._entities[key] = current
        return dict(current)

    def _ingest_read_data(self, read_data, now):
        changed = []
        for item in (read_data.get("OUTPUTS") or []):
            changed.append(self._upsert("outputs", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("BUS_HAS") or []):
            if (item.get("TYP") == "DOMUS") or ("DOMUS" in str(item.get("TYP", ""))):
                changed.append(self._upsert("domus", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("POWER_LINES") or []):
            changed.append(self._upsert("powerlines", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("PARTITIONS") or []):
            changed.append(self._upsert("partitions", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("ZONES") or []):
            changed.append(self._upsert("zones", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("SCENARIOS") or []):
            changed.append(self._upsert("scenarios", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("STATUS_SYSTEM") or []):
            changed.append(self._upsert("systems", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("STATUS_CONNECTION") or []):
            changed.append(self._upsert("connection", item.get("ID"), {"static": item}, now))
            gsm = _gsm_from_connection_item(item)
            if gsm:
                changed.append(self._upsert("gsm", gsm.get("ID"), {"static": gsm}, now))
        for item in (read_data.get("CFG_THERMOSTATS") or []):
            changed.append(self._upsert("thermostats", item.get("ID"), {"static": item}, now))
        for item in (read_data.get("CFG_ACCOUNTS") or []):
            changed.append(self._upsert("accounts", item.get("ID"), {"static": item}, now))
        return [c for c in changed if c]

    def _ingest_realtime_payload(self, payload, now):
        changed = []
        for item in (payload.get("STATUS_OUTPUTS") or []):
            changed.append(self._upsert("outputs", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_BUS_HA_SENSORS") or []):
            changed.append(self._upsert("domus", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_POWER_LINES") or []):
            changed.append(self._upsert("powerlines", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_PARTITIONS") or []):
            changed.append(self._upsert("partitions", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_ZONES") or []):
            changed.append(self._upsert("zones", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_SYSTEM") or []):
            changed.append(self._upsert("systems", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_CONNECTION") or []):
            changed.append(self._upsert("connection", item.get("ID"), {"realtime": item}, now))
            gsm = _gsm_from_connection_item(item)
            if gsm:
                changed.append(self._upsert("gsm", gsm.get("ID"), {"realtime": gsm}, now))
        for item in (payload.get("STATUS_TEMPERATURES") or []):
            changed.append(self._upsert("thermostats", item.get("ID"), {"realtime": item}, now))
        for item in (payload.get("STATUS_HUMIDITY") or []):
            changed.append(self._upsert("thermostats", item.get("ID"), {"realtime": item}, now))
        return [c for c in changed if c]


def _infer_name(data):
    if not isinstance(data, dict):
        return None
    for key in ("EV", "NM", "LBL", "DES", "DESCR", "DESC", "NAME", "LABEL", "NME"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _infer_access(entity_type: str) -> str:
    if entity_type in ("outputs", "scenarios", "partitions", "zones", "thermostats", "accounts"):
        return "rw"
    return "r"


def _coerce_bool(value, default=True):
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", ""):
        return False
    return bool(default)


def _load_ui_tags(path=_UI_TAGS_PATH):
    data = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in ("outputs", "scenarios", "tag_styles"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    # Seed default tag styles if none are configured yet (safe: user can edit/remove).
    if not data.get("tag_styles"):
        data["tag_styles"] = {
            "Cancelli": {"icon_off": "mdiGate", "icon_on": "mdiGate", "color_off": "#a9b1c3", "color_on": "#1ed760"},
            "barre": {"icon_off": "mdiBoomGate", "icon_on": "mdiBoomGate", "color_off": "#a9b1c3", "color_on": "#ffb020"},
            "Portoni": {
                "icon_off": "mdiGarageVariant",
                "icon_on": "mdiGarageOpenVariant",
                "color_off": "#a9b1c3",
                "color_on": "#1ed760",
            },
            "Grid": {"icon_off": "mdiGridLarge", "icon_on": "mdiGridLarge", "color_off": "#a9b1c3", "color_on": "#39a0ff"},
            "tende": {
                "icon_off": "mdiCurtainsClosed",
                "icon_on": "mdiCurtains",
                "color_off": "#a9b1c3",
                "color_on": "#1ed760",
            },
            "Luci": {"icon_off": "mdiLightbulb", "icon_on": "mdiLightbulb", "color_off": "#a9b1c3", "color_on": "#ffd24a"},
            "blind": {
                "icon_off": "mdiBlindsHorizontalClosed",
                "icon_on": "mdiBlindsHorizontal",
                "color_off": "#a9b1c3",
                "color_on": "#1ed760",
            },
            "roller": {
                "icon_off": "mdiRollerShadeClosed",
                "icon_on": "mdiRollerShade",
                "color_off": "#a9b1c3",
                "color_on": "#1ed760",
            },
            "tapparelle": {
                "icon_off": "mdiWindowShutter",
                "icon_on": "mdiWindowShutterOpen",
                "color_off": "#a9b1c3",
                "color_on": "#1ed760",
            },
            "shutter": {
                "icon_off": "mdiWindowShutter",
                "icon_on": "mdiWindowShutterOpen",
                "color_off": "#a9b1c3",
                "color_on": "#1ed760",
            },
            "Pompe": {"icon_off": "mdiPump", "icon_on": "mdiPump", "color_off": "#1ed760", "color_on": "#ff4d4d"},
        }
    return data


def _load_ui_favorites(path=_UI_FAVORITES_PATH):
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in ("outputs", "scenarios", "zones", "partitions"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _save_ui_favorites(data, path=_UI_FAVORITES_PATH):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_ui_thermostat_names(path=_UI_THERM_NAMES_PATH):
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            sid = str(k)
            if isinstance(v, str) and v.strip():
                out[sid] = v.strip()
        return out
    except Exception:
        return {}


def _save_ui_thermostat_names(data: dict, path=_UI_THERM_NAMES_PATH):
    try:
        if not isinstance(data, dict):
            data = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _UI_THERM_NAMES_CACHE["data"] = dict(data)
        _UI_THERM_NAMES_CACHE["ts"] = time.time()
        return True
    except Exception:
        return False


def _get_ui_tag(ui_tags, entity_type, entity_id):
    t = str(entity_type or "").lower()
    data = ui_tags.get(t) if isinstance(ui_tags, dict) else {}
    if not isinstance(data, dict):
        data = {}
    entry = data.get(str(entity_id)) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return "", True
    tag = entry.get("tag")
    if not isinstance(tag, str):
        tag = ""
    visible = _coerce_bool(entry.get("visible", True), True)
    return tag.strip(), visible


def _slugify_tag(tag: str) -> str:
    s = str(tag or "").strip().lower()
    out = []
    prev_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    slug = "".join(out).strip("-")
    return slug or "senza-tag"


def _icon_svg(kind: str) -> str:
    k = str(kind or "").strip().lower()
    if k == "light":
        return (
            "<svg width=\"20\" height=\"20\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\">"
            "<path d=\"M9 21h6m-5-3h4\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\"/>"
            "<path d=\"M12 3a6 6 0 0 0-3 11.2V17h6v-2.8A6 6 0 0 0 12 3Z\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linejoin=\"round\"/>"
            "</svg>"
        )
    if k == "blinds":
        return (
            "<svg width=\"20\" height=\"20\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\">"
            "<path d=\"M6 7h12M6 11h12M6 15h12\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\"/>"
            "<path d=\"M8 19h8\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\"/>"
            "</svg>"
        )
    if k == "gate":
        return (
            "<svg width=\"20\" height=\"20\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\">"
            "<path d=\"M5 21V7l7-3 7 3v14\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linejoin=\"round\"/>"
            "<path d=\"M9 21V11h6v10\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linejoin=\"round\"/>"
            "</svg>"
        )
    return (
        "<svg width=\"20\" height=\"20\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\">"
        "<path d=\"M7 7h10v10H7z\" stroke=\"currentColor\" stroke-width=\"1.8\"/>"
        "<path d=\"M5 12h2M17 12h2M12 5v2M12 17v2\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\"/>"
        "</svg>"
    )


def _fmt_ts(ts):
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return str(ts)


def _html_escape(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _static_field_info(entity_type: str, key: str, value):
    t = (entity_type or "").lower()
    k = str(key or "")

    def _bool_it(v):
        sv = str(v).upper()
        if sv == "T":
            return "Sì"
        if sv == "F":
            return "No"
        return None

    if t == "zones":
        info = {
            "ID": "ID della zona",
            "DES": "Descrizione della zona",
            "CAT": "Categoria/tipo zona (es. IMOV=movimento)",
            "PRT": "Maschera partizioni associate (bitmask)",
            "BYP_EN": "Bypass abilitabile",
            "CMD": "Zona comandabile (supporta comandi)",
            "AN": "Zona analogica",
        }.get(k)
        if k in ("BYP_EN", "CMD", "AN"):
            human = _bool_it(value)
            if human:
                return f"{info or ''} ({human})".strip()
        return info

    if t == "partitions":
        info = {
            "ID": "ID della partizione",
            "DES": "Descrizione della partizione",
            "ZONES": "Elenco zone associate alla partizione",
        }.get(k)
        return info

    if t == "outputs":
        info = {
            "ID": "ID uscita",
            "DES": "Descrizione uscita",
            "CAT": "Categoria (LIGHT/ROLL/...)",
        }.get(k)
        return info

    if t == "thermostats":
        info = {
            "ID": "ID termostato",
            "ACT_MODE": "Modalità attiva (es. OFF/MAN/AUTO)",
            "ACT_SEA": "Stagione attiva (WIN=inverno, SUM=estate)",
            "MAN_HRS": "Durata manuale (ore) se prevista",
            "WIN": "Profili temperatura inverno (T1/T2/T3/TM + programmazione oraria)",
            "SUM": "Profili temperatura estate (T1/T2/T3/TM + programmazione oraria)",
            "TOF": "Isteresi/parametri termostato (se presenti)",
        }.get(k)
        return info


def _thermostat_mode_options(static: dict, current_mode: str) -> list[str]:
    options = []
    def _norm(m: str) -> str:
        m = str(m or "").strip().upper()
        return "WEEKLY" if m == "AUTO" else m

    for m in ("OFF", "MAN", "MAN_TMR", "WEEKLY", "SD1", "SD2"):
        options.append(m)
    try:
        st_mode = _norm(static.get("ACT_MODE") or "") if isinstance(static, dict) else ""
    except Exception:
        st_mode = ""
    if st_mode and st_mode not in options:
        options.append(st_mode)
    cur = _norm(current_mode)
    if cur and cur not in options:
        options.insert(0, cur)
    # Unique, preserve order
    seen = set()
    out = []
    for m in options:
        if not m:
            continue
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out

    if t == "domus":
        info = {
            "ID": "ID periferica",
            "SN": "Seriale periferica",
            "BUS": "Numero bus",
            "TYP": "Tipo periferica (DOMUS)",
            "FW": "Versione firmware",
            "INFO": "Informazioni periferica",
        }.get(k)
        return info

    if t == "connection":
        info = {
            "ID": "ID stato connessione",
            "MOBILE": "Dati modem GSM/SIM (operatore, segnale, credito, ecc.)",
        }.get(k)
        return info

    if t == "gsm":
        if k == "SIGNAL":
            return "Segnale (0-31)"
        info = {
            "ID": "ID modem GSM",
            "CARRIER": "Operatore GSM",
            "SIGNAL": "Segnale (0–31)",
            "SIGNAL_PCT": "Segnale (%)",
            "SSIM": "Stato SIM",
            "CRE": "Credito (se disponibile)",
            "EXPIR": "Scadenza SIM/credito (se disponibile)",
            "LASTERR": "Ultimo errore modem",
            "BOARD": "Info modulo (MOD/IMEI/VERS)",
        }.get(k)
        return info

    if t == "systems":
        info = {
            "ID": "ID sistema",
            "ARM": "Stato inserimento del sistema (S=codice, D=descrizione)",
            "TEMP": "Temperature (IN/OUT) se disponibili",
            "TIME": "Info orario (GMT/TZ/alba/tramonto)",
            "INFO": "Informazioni/flag di sistema",
            "FAULT": "Guasti attivi",
            "FAULT_MEM": "Memoria guasti",
            "TAMPER": "Sabotaggi attivi",
            "TAMPER_MEM": "Memoria sabotaggi",
            "ALARM": "Allarmi attivi",
            "ALARM_MEM": "Memoria allarmi",
            "MODEL": "Modello centrale",
            "BRAND": "Brand produttore",
            "BOOT": "Versione bootloader",
            "IP": "Versione firmware IP",
            "FS": "Versione filesystem",
            "SSL": "Versione SSL",
            "OS": "Versione sistema operativo",
            "VER_LITE": "Versioni (FW/WS/VM/...)",
            "PRG_CHECK": "Checksum/config (PRG/CFG)",
        }.get(k)
        return info

    if t == "logs":
        info = {
            "ID": "ID evento",
            "DATA": "Data (gg/mm/aaaa)",
            "TIME": "Ora (hh:mm:ss)",
            "TYPE": "Categoria evento (codice)",
            "EV": "Evento (descrizione)",
            "I1": "Info 1 (es. nome uscita/utente/partizione)",
            "I2": "Info 2 (es. origine/IP/dettagli)",
            "IML": "Immagine allegata (T/F)",
        }.get(k)
        return info

    if t == "schedulers":
        info = {
            "ID": "ID programmatore",
            "DES": "Descrizione",
            "EN": "Abilitato (T/F)",
            "TYPE": "Tipo (TIME=orario)",
            "H": "Ora",
            "M": "Minuti",
            "MON": "Lunedì (T/F)",
            "TUE": "Martedì (T/F)",
            "WED": "Mercoledì (T/F)",
            "THU": "Giovedì (T/F)",
            "FRI": "Venerdì (T/F)",
            "SAT": "Sabato (T/F)",
            "SUN": "Domenica (T/F)",
            "EXCL_HOLIDAYS": "Escludi nei festivi (T/F)",
            "PRT": "Partizioni abilitate (ALL o mask)",
            "SCE": "ID scenario da eseguire",
            "SCE_NAME": "Nome scenario",
        }.get(k)
        return info

    return None


def _format_temp(value):
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s or s.upper() == "NA":
        return None
    # Some panels send e.g. "NA|24.5"
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        if parts:
            s = parts[-1]
    s = s.replace(",", ".").strip()
    if s.startswith("+"):
        s = s[1:]
    try:
        n = float(s)
    except Exception:
        return None
    return f"{n:.1f}°C"


def _system_arm_status(arm_obj):
    if not isinstance(arm_obj, dict):
        return None, None, None
    s = arm_obj.get("S")
    d = arm_obj.get("D")
    s_code = str(s or "").strip().upper()
    status_map = {
        "D": "Disinserito",
        "T": "Inserito totale",
        "T_IN": "Inserito totale (ingresso)",
        "T_OUT": "Inserito totale (uscita)",
        "P": "Inserito parziale",
        "P_IN": "Inserito parziale (ingresso)",
        "P_OUT": "Inserito parziale (uscita)",
    }
    status = status_map.get(s_code, s_code or None)
    desc = str(d).strip() if isinstance(d, str) and d.strip() else None
    return status, desc, s_code or None


def _pos_to_pct(value):
    if value in (None, ""):
        return None
    try:
        n = int(float(value))
    except Exception:
        return None
    if n > 100:
        n = max(0, min(255, n))
        return int(round((n / 255) * 100))
    return max(0, min(100, n))


def _signal_to_percent(value):
    if value in (None, ""):
        return None
    try:
        n = float(str(value).strip().replace(",", "."))
    except Exception:
        return None
    if n < 0:
        return None
    if n <= 31:
        return int(round((n / 31.0) * 100))
    if n <= 100:
        return int(round(n))
    return None


def _gsm_from_connection_item(item):
    if not isinstance(item, dict):
        return None
    mobile = item.get("MOBILE")
    if not isinstance(mobile, dict):
        return None
    gsm = {"ID": item.get("ID")}
    gsm.update(mobile)
    pct = _signal_to_percent(mobile.get("SIGNAL"))
    if pct is not None:
        gsm["SIGNAL_PCT"] = pct
    return gsm


def _row_state_class(entity_type: str, static: dict, realtime: dict) -> str:
    t = str(entity_type or "").lower()
    static = static or {}
    realtime = realtime or {}

    if t == "memoria_allarmi":
        return "st-zone-alarm"

    if t == "outputs":
        cat = str(static.get("CAT") or static.get("TYP") or "").upper()
        sta = str(realtime.get("STA") or "").upper()
        if cat == "ROLL":
            pct = _pos_to_pct(realtime.get("POS"))
            if pct is not None and pct > 0:
                return "st-roll-open"
            # also consider movement state
            if sta in ("UP", "DOWN"):
                return "st-roll-open"
            return ""
        if sta == "ON":
            return "st-light-on"
        return ""

    if t == "zones":
        sta = str(realtime.get("STA") or "").upper()
        if sta == "R":
            return "st-zone-rest"
        if sta == "A":
            return "st-zone-alarm"
        if sta == "T":
            return "st-zone-tamper"
        if sta == "FM":
            return "st-zone-mask"
        return ""

    if t == "partitions":
        arm = str(realtime.get("ARM") or "").upper()
        # D = disarmed, IA = instant armed, DA/OT/IT = delays.
        if arm == "D":
            return "st-part-disarmed"
        if arm == "IA":
            return "st-part-armed"
        if arm in ("DA", "OT", "IT"):
            return "st-part-delayed"
        return ""

    if t == "thermostats":
        therm = realtime.get("THERM") if isinstance(realtime, dict) else None
        mode = ""
        sea = ""
        out = ""
        if isinstance(therm, dict):
            mode = str(therm.get("ACT_MODEL") or therm.get("ACT_MODE") or "").upper()
            sea = str(therm.get("ACT_SEA") or "").upper()
            out = str(therm.get("OUT_STATUS") or "").upper()
        if not mode:
            mode = str(static.get("ACT_MODE") or "").upper()
        if not sea:
            sea = str(static.get("ACT_SEA") or "").upper()

        bg = ""
        if mode == "OFF":
            bg = "st-therm-off"
        elif mode == "MAN":
            bg = "st-therm-man-win" if sea == "WIN" else "st-therm-man-sum"
        elif mode == "MAN_TMR":
            bg = "st-therm-mantmr-win" if sea == "WIN" else "st-therm-mantmr-sum"
        elif mode == "SD1":
            bg = "st-therm-sd1"
        elif mode == "SD2":
            bg = "st-therm-sd2"
        elif mode == "WEEKLY":
            bg = "st-therm-auto"

        if out == "ON":
            return (bg + " st-therm-out-on").strip()
        return bg

    return ""


def _render_kv_table(
    data,
    *,
    entity_type: str | None = None,
    kind: str | None = None,
    partition_names: dict[int, str] | None = None,
):
    if not isinstance(data, dict) or not data:
        return "<span class=\"muted\">-</span>"
    items = []
    for key in sorted(data.keys(), key=lambda x: str(x)):
        value = data.get(key)
        info = None
        if kind == "static" and entity_type:
            info = _static_field_info(entity_type, key, value)
            if (
                entity_type == "zones"
                and str(key) == "PRT"
                and partition_names
                and isinstance(partition_names, dict)
            ):
                mask_str = "" if value is None else str(value).strip()
                mask_up = mask_str.upper()
                ids = []
                if mask_up in ("ALL", "ALLALL"):
                    ids = sorted(partition_names.keys())
                else:
                    max_id = max(partition_names.keys()) if partition_names else 0
                    max_id = max(max_id, 20)

                    def _decode(mask: int):
                        out = []
                        for pid in range(1, max_id + 1):
                            if mask & (1 << (pid - 1)):
                                out.append(pid)
                        return out

                    # Some panels/UI seem to represent the partition mask as a hex string (e.g. "100" => 0x100).
                    candidates = []
                    dec_mask = None
                    hex_mask = None
                    try:
                        dec_mask = int(float(mask_str))
                        if dec_mask >= 0:
                            candidates.append(("dec", dec_mask))
                    except Exception:
                        dec_mask = None

                    # Many panels send PRT as a hex bitmask without the "0x" prefix (even for short values like "40").
                    is_hexish = all(c in "0123456789abcdefABCDEF" for c in mask_str) and len(mask_str) >= 2
                    if mask_up.startswith("0X"):
                        try:
                            hex_mask = int(mask_str, 16)
                            if hex_mask >= 0:
                                candidates.append(("hex", hex_mask))
                                is_hexish = True
                        except Exception:
                            hex_mask = None
                    elif is_hexish:
                        try:
                            hex_mask = int(mask_str, 16)
                            if hex_mask >= 0:
                                candidates.append(("hex", hex_mask))
                        except Exception:
                            hex_mask = None

                    chosen = None
                    chosen_ids = []
                    if candidates:
                        # Prefer the interpretation that produces a *reasonable* number of partitions.
                        scored = []
                        for fmt, m in candidates:
                            decoded = _decode(m)
                            if not decoded:
                                continue
                            # score: prefer fewer partitions (often single partition), but avoid obviously wrong empties.
                            scored.append((len(decoded), 0 if fmt == "hex" else 1, fmt, decoded, m))
                        if scored:
                            scored.sort()
                            _, _, chosen, chosen_ids, chosen_mask = scored[0]
                            ids = chosen_ids
                            # Add a hint if we picked hex over decimal.
                            # Keep UI clean: don't show parsing details by default.
                        else:
                            ids = []
                if ids:
                    parts = []
                    for pid in ids:
                        name = partition_names.get(pid) or f"Partizione {pid}"
                        parts.append(f"{pid}: {name}")
                    if len(ids) == 1:
                        extra = f"Maschera: {partition_names.get(ids[0]) or f'Partizione {ids[0]}'}"
                    else:
                        extra = "Maschera: " + ", ".join(
                            [(partition_names.get(pid) or f"Partizione {pid}") for pid in ids]
                        )
                    info = extra
        info_cell = _html_escape(info) if info else '<span class="muted">-</span>'
        if isinstance(value, (dict, list)):
            value_str = (
                "<pre class=\"json\">"
                + _html_escape(json.dumps(value, ensure_ascii=False, indent=2))
                + "</pre>"
            )
        else:
            value_str = _html_escape("" if value is None else str(value))
        key_attr = _html_escape(str(key))
        items.append(
            f"<tr data-kv-key=\"{key_attr}\">"
            f"<td class=\"k\">{_html_escape(key)}</td>"
            f"<td class=\"v\" data-kv-key=\"{key_attr}\">{value_str}</td>"
            f"<td class=\"i\">{info_cell}</td>"
            "</tr>"
        )
    kind_attr = _html_escape(str(kind or ""))
    return f"<table class=\"kv\" data-kv-kind=\"{kind_attr}\">{''.join(items)}</table>"


def render_index(snapshot):
    entities = snapshot.get("entities") or []
    ui_tags = _load_ui_tags()
    try:
        tag_styles = ui_tags.get("tag_styles") if isinstance(ui_tags, dict) else {}
        if not isinstance(tag_styles, dict):
            tag_styles = {}
        tag_style_names = sorted(
            [k for k in tag_styles.keys() if isinstance(k, str) and k.strip()],
            key=lambda s: s.lower(),
        )
    except Exception:
        tag_style_names = []
    tag_style_names_json = json.dumps(tag_style_names, ensure_ascii=False)
    def _is_alarm_log(rt: dict) -> bool:
        typ = str(rt.get("TYPE") or "").upper()
        ev = str(rt.get("EV") or "").lower()
        if ev and "ripristino" in ev:
            return False
        if ev and ("tempo d'uscita" in ev or "tempo d'ingresso" in ev):
            return False
        if "allarme zona" in ev:
            return True
        if typ in ("ZALARM", "ALARM"):
            return True
        return False

    def _is_alarm_reset_log(rt: dict) -> bool:
        typ = str(rt.get("TYPE") or "").upper()
        ev = str(rt.get("EV") or "").lower()
        if "reset allarmi" in ev:
            return True
        if "reset allarme" in ev:
            return True
        if "reset memoria" in ev:
            return True
        if "ripristino memoria" in ev:
            return True
        if "reset" in typ:
            return True
        return False
    scenario_names = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "scenarios":
            continue
        try:
            sid = int(e.get("id"))
        except Exception:
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        name = st.get("DES") or st.get("NM") or e.get("name")
        if isinstance(name, str) and name.strip():
            scenario_names[sid] = name.strip()
    partition_names = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "partitions":
            continue
        try:
            pid = int(e.get("id"))
        except Exception:
            continue
        name = None
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        for src in (st, rt):
            des = src.get("DES")
            if isinstance(des, str) and des.strip():
                name = des.strip()
                break
        if not name:
            name = e.get("name")
        if isinstance(name, str) and name.strip():
            partition_names[pid] = name.strip()

    # Build a reverse mapping: partition_id -> zones associated (based on zone static PRT mask).
    def _decode_zone_prt_mask(value):
        mask_str = "" if value is None else str(value).strip()
        mask_up = mask_str.upper()
        if mask_up in ("ALL", "ALLALL"):
            return sorted(partition_names.keys()) if partition_names else []
        max_id = max(partition_names.keys()) if partition_names else 0
        max_id = max(max_id, 20)

        def _decode(mask: int):
            out = []
            for pid in range(1, max_id + 1):
                if mask & (1 << (pid - 1)):
                    out.append(pid)
            return out

        candidates = []
        try:
            dec_mask = int(float(mask_str))
            if dec_mask >= 0:
                candidates.append(("dec", dec_mask))
        except Exception:
            pass

        is_hexish = all(c in "0123456789abcdefABCDEF" for c in mask_str) and len(mask_str) >= 2
        if mask_up.startswith("0X"):
            try:
                candidates.append(("hex", int(mask_str, 16)))
            except Exception:
                pass
        elif is_hexish:
            try:
                candidates.append(("hex", int(mask_str, 16)))
            except Exception:
                pass

        scored = []
        for fmt, m in candidates:
            decoded = _decode(m)
            if not decoded:
                continue
            scored.append((len(decoded), 0 if fmt == "hex" else 1, decoded))
        if not scored:
            return []
        scored.sort()
        return scored[0][2]

    zones_by_partition = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "zones":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        zid = e.get("id")
        zname = st.get("DES") or e.get("name") or f"Zona {zid}"
        pids = _decode_zone_prt_mask(st.get("PRT"))
        for pid in pids:
            zones_by_partition.setdefault(pid, []).append({"ID": zid, "DES": zname})
    # Sort zones by ID for each partition.
    for pid, zlist in zones_by_partition.items():
        try:
            zlist.sort(key=lambda x: int(str(x.get("ID") or 0)))
        except Exception:
            pass

    # Build "memoria allarmi" entries from logs since last reset.
    reset_ts = 0
    reset_id = 0
    for e in entities:
        if str(e.get("type") or "").lower() != "logs":
            continue
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        if not _is_alarm_reset_log(rt):
            continue
        ts = e.get("last_seen") or 0
        try:
            ts = int(ts)
        except Exception:
            ts = 0
        if ts > reset_ts:
            reset_ts = ts
        rid = rt.get("ID")
        try:
            rid = int(rid)
        except Exception:
            rid = 0
        if rid > reset_id:
            reset_id = rid

    mem_entities = []
    for e in entities:
        if str(e.get("type") or "").lower() != "logs":
            continue
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        if not _is_alarm_log(rt):
            continue
        ts = e.get("last_seen") or 0
        try:
            ts = int(ts)
        except Exception:
            ts = 0
        rid = rt.get("ID")
        try:
            rid = int(rid)
        except Exception:
            rid = 0
        if reset_id and rid and rid < reset_id:
            continue
        if reset_ts and ts < reset_ts:
            continue
        name = str(rt.get("I1") or rt.get("EV") or "Allarme zona")
        mem_entities.append(
            {
                "type": "memoria_allarmi",
                "id": ts or rt.get("ID") or 0,
                "access": "r",
                "name": name,
                "static": {},
                "realtime": rt,
                "last_seen": e.get("last_seen"),
            }
        )

    # Always include the list entries (may be empty); we still want the filter visible.
    entities = list(entities) + mem_entities

    def _sort_key(e):
        t = str(e.get("type") or "unknown")
        try:
            entity_id = int(e.get("id") or 0)
        except Exception:
            entity_id = 0
        if t.lower() == "logs":
            entity_id = -entity_id
        return (t, entity_id)

    entities = sorted(entities, key=_sort_key)
    meta = snapshot.get("meta") or {}

    counts = {}
    for e in entities:
        t = str(e.get("type") or "unknown")
        counts[t] = counts.get(t, 0) + 1
    counts.setdefault("memoria_allarmi", 0)

    preferred_order = [
        "outputs",
        "partitions",
        "logs",
        "memoria_allarmi",
        "schedulers",
        "scenarios",
        "systems",
        "thermostats",
        "connection",
        "gsm",
        "zones",
        "domus",
        "powerlines",
    ]
    type_order = [t for t in preferred_order if t in counts]
    type_order.extend(sorted([t for t in counts.keys() if t not in preferred_order]))

    rows = []
    for e in entities:
        entity_type = e.get("type")
        entity_id = e.get("id")
        access = e.get("access")
        name = e.get("name") or ""
        if (not name) and str(entity_type).lower() == "thermostats" and entity_id not in (None, ""):
            name = f"Termostato {entity_id}"
        static = e.get("static") if isinstance(e.get("static"), dict) else {}
        realtime = e.get("realtime") or {}
        cat = static.get("CAT") or static.get("TYP") or ""
        tag_value = ""
        visible_flag = True
        if str(entity_type).lower() in ("outputs", "scenarios"):
            tag_value, visible_flag = _get_ui_tag(ui_tags, entity_type, entity_id)
        sta = realtime.get("STA") if isinstance(realtime, dict) else ""
        pos = realtime.get("POS") if isinstance(realtime, dict) else ""
        arm = realtime.get("ARM") if isinstance(realtime, dict) else ""
        ast = realtime.get("AST") if isinstance(realtime, dict) else ""
        raw_time = None
        if isinstance(realtime, dict):
            raw_time = realtime.get("TIME")
            if raw_time in (None, ""):
                raw_time = realtime.get("T")

        summary_parts = []
        if str(entity_type).lower() == "partitions":
            tst = realtime.get("TST") if isinstance(realtime, dict) else ""
            arm_code = str(arm or "").upper()
            ast_code = str(ast or "").upper()
            tst_code = str(tst or "").upper()

            arm_map = {
                "D": "Disinserita",
                "DA": "Inserimento ritardato (uscita)",
                "IA": "Inserita (istantanea)",
                "IT": "Ingresso in corso",
                "OT": "Uscita in corso",
            }
            ast_map = {
                "OK": "Nessun allarme",
                "AL": "Allarme attivo",
                "AM": "Memoria allarme",
            }
            tst_map = {
                "OK": "Nessun sabotaggio",
                "TAM": "Sabotaggio in corso",
                "AM": "Memoria sabotaggio",
            }

            if arm_code:
                summary_parts.append(f"Stato: {arm_map.get(arm_code, arm_code)} ({arm_code})")
            if ast_code:
                summary_parts.append(f"Allarme: {ast_map.get(ast_code, ast_code)} ({ast_code})")
            if tst_code:
                summary_parts.append(f"Sabotaggio: {tst_map.get(tst_code, tst_code)} ({tst_code})")

            if raw_time not in (None, ""):
                if arm_code == "IT":
                    summary_parts.append(f"Tempo ingresso: {raw_time}")
                elif arm_code in ("OT", "DA"):
                    summary_parts.append(f"Tempo uscita: {raw_time}")
                else:
                    summary_parts.append(f"Tempo: {raw_time}")
        elif str(entity_type).lower() == "zones":
            byp = realtime.get("BYP") if isinstance(realtime, dict) else ""
            tmem = realtime.get("T") if isinstance(realtime, dict) else ""

            sta_code = str(sta or "").upper()
            byp_code = str(byp or "").upper()
            tmem_code = str(tmem or "").upper()

            sta_map = {
                "R": "Normale",
                "A": "Allarme",
                "FM": "Mascheramento/Guasto",
                "T": "Sabotaggio",
                "E": "Errore",
            }
            byp_map = {
                "NO": "Non esclusa",
                "UN_BYPASS": "Non esclusa",
                "AUTO": "Esclusa (auto)",
                "MAN_I": "Esclusa (manuale ingresso)",
                "MAN_M": "Esclusa (manuale menu)",
                "BYPASS": "Esclusa",
            }

            if sta_code:
                summary_parts.append(f"Stato: {sta_map.get(sta_code, sta_code)} ({sta_code})")
            if byp_code:
                summary_parts.append(f"Esclusione: {byp_map.get(byp_code, byp_code)} ({byp_code})")
            if tmem_code not in (None, "", "0", "N", "NO"):
                summary_parts.append(f"Memoria: {tmem_code}")
        elif str(entity_type).lower() == "systems":
            arm_obj = realtime.get("ARM") if isinstance(realtime, dict) else None
            if not arm_obj and isinstance(static, dict):
                arm_obj = static.get("ARM")
            status, desc, s_code = _system_arm_status(arm_obj)
            if status:
                if s_code:
                    summary_parts.append(f"Stato: {status} ({s_code})")
                else:
                    summary_parts.append(f"Stato: {status}")
            if desc:
                summary_parts.append(f"Modalità: {desc}")

            temp_obj = realtime.get("TEMP") if isinstance(realtime, dict) else None
            if not temp_obj and isinstance(static, dict):
                temp_obj = static.get("TEMP")
            if isinstance(temp_obj, dict):
                tin = _format_temp(temp_obj.get("IN"))
                tout = _format_temp(temp_obj.get("OUT"))
                if tin:
                    summary_parts.append(f"Temp interna: {tin}")
                if tout:
                    summary_parts.append(f"Temp esterna: {tout}")

            model = (realtime.get("MODEL") if isinstance(realtime, dict) else None) or (
                static.get("MODEL") if isinstance(static, dict) else None
            )
            ver = (realtime.get("VER_LITE") if isinstance(realtime, dict) else None) or (
                static.get("VER_LITE") if isinstance(static, dict) else None
            )
            fw = None
            ws = None
            if isinstance(ver, dict):
                fw = ver.get("FW")
                ws = ver.get("WS")
            if model:
                summary_parts.append(f"Modello: {model}")
            if fw:
                summary_parts.append(f"FW: {fw}")
            if ws:
                summary_parts.append(f"WS: {ws}")
        elif str(entity_type).lower() == "logs":
            ev = (realtime.get("EV") if isinstance(realtime, dict) else None) or (
                static.get("EV") if isinstance(static, dict) else None
            )
            info1 = (realtime.get("I1") if isinstance(realtime, dict) else None) or (
                static.get("I1") if isinstance(static, dict) else None
            )
            info2 = (realtime.get("I2") if isinstance(realtime, dict) else None) or (
                static.get("I2") if isinstance(static, dict) else None
            )
            typ = (realtime.get("TYPE") if isinstance(realtime, dict) else None) or (
                static.get("TYPE") if isinstance(static, dict) else None
            )
            d = (realtime.get("DATA") if isinstance(realtime, dict) else None) or (
                static.get("DATA") if isinstance(static, dict) else None
            )
            t = (realtime.get("TIME") if isinstance(realtime, dict) else None) or (
                static.get("TIME") if isinstance(static, dict) else None
            )
            if d and t:
                summary_parts.append(f"Quando: {d} {t}")
            elif d:
                summary_parts.append(f"Data: {d}")
            elif t:
                summary_parts.append(f"Ora: {t}")
            if ev:
                summary_parts.append(f"Evento: {ev}")
            if info1:
                summary_parts.append(f"Info: {info1}")
            if info2:
                summary_parts.append(f"Dettagli: {info2}")
            if typ:
                summary_parts.append(f"Tipo: {typ}")
        elif str(entity_type).lower() == "memoria_allarmi":
            ev = realtime.get("EV") if isinstance(realtime, dict) else None
            info1 = realtime.get("I1") if isinstance(realtime, dict) else None
            info2 = realtime.get("I2") if isinstance(realtime, dict) else None
            d = realtime.get("DATA") if isinstance(realtime, dict) else None
            t = realtime.get("TIME") if isinstance(realtime, dict) else None
            if d and t:
                summary_parts.append(f"Quando: {d} {t}")
            elif d:
                summary_parts.append(f"Data: {d}")
            elif t:
                summary_parts.append(f"Ora: {t}")
            if ev:
                summary_parts.append(f"Evento: {ev}")
            if info1:
                summary_parts.append(f"Zona: {info1}")
            if info2:
                summary_parts.append(f"Dettagli: {info2}")
        elif str(entity_type).lower() == "schedulers":
            en = (static.get("EN") if isinstance(static, dict) else None) or (
                realtime.get("EN") if isinstance(realtime, dict) else None
            )
            h = (static.get("H") if isinstance(static, dict) else None) or (
                realtime.get("H") if isinstance(realtime, dict) else None
            )
            m = (static.get("M") if isinstance(static, dict) else None) or (
                realtime.get("M") if isinstance(realtime, dict) else None
            )
            sce = (static.get("SCE") if isinstance(static, dict) else None) or (
                realtime.get("SCE") if isinstance(realtime, dict) else None
            )
            sce_name = None
            try:
                if sce is not None:
                    sce_name = scenario_names.get(int(str(sce)))
            except Exception:
                sce_name = None
            if str(en).upper() in ("T", "TRUE", "1"):
                summary_parts.append("Abilitato: Sì")
            elif en is not None:
                summary_parts.append("Abilitato: No")
            if h is not None and m is not None:
                summary_parts.append(f"Orario: {int(h):02d}:{int(m):02d}")
            days = []
            day_map = [
                ("MON", "Lun"),
                ("TUE", "Mar"),
                ("WED", "Mer"),
                ("THU", "Gio"),
                ("FRI", "Ven"),
                ("SAT", "Sab"),
                ("SUN", "Dom"),
            ]
            for k, lab in day_map:
                v = (static.get(k) if isinstance(static, dict) else None) or (
                    realtime.get(k) if isinstance(realtime, dict) else None
                )
                if str(v).upper() == "T":
                    days.append(lab)
            if days:
                summary_parts.append("Giorni: " + ", ".join(days))
            if sce_name:
                summary_parts.append(f"Scenario: {sce_name}")
            elif sce is not None and str(sce) != "":
                summary_parts.append(f"Scenario ID: {sce}")
        elif str(entity_type).lower() == "domus":
            dom = None
            if isinstance(realtime, dict):
                dom = realtime.get("DOMUS") if isinstance(realtime.get("DOMUS"), dict) else realtime
            if isinstance(dom, dict):
                tem = _format_temp(dom.get("TEM"))
                hum = dom.get("HUM")
                lht = dom.get("LHT")
                pir = str(dom.get("PIR") or "").upper()
                tl = str(dom.get("TL") or "").upper()
                th = str(dom.get("TH") or "").upper()
                sta_dom = str(realtime.get("STA") or "").upper() if isinstance(realtime, dict) else ""

                if tem:
                    summary_parts.append(f"Temperatura: {tem}")
                if hum not in (None, "", "NA"):
                    summary_parts.append(f"Umidità: {hum}%")
                if lht not in (None, "", "NA"):
                    summary_parts.append(f"Luminosità: {lht} lx")
                if pir in ("T", "F"):
                    summary_parts.append(f"Movimento: {'Sì' if pir == 'T' else 'No'}")
                if tl in ("T", "F"):
                    summary_parts.append(f"Soglia luce: {'Attiva' if tl == 'T' else 'OK'}")
                if th in ("T", "F"):
                    summary_parts.append(f"Soglia umidità: {'Attiva' if th == 'T' else 'OK'}")
                if sta_dom:
                    summary_parts.append(f"Stato: {sta_dom}")
        elif str(entity_type).lower() == "connection":
            mobile = None
            if isinstance(realtime, dict):
                m = realtime.get("MOBILE")
                if isinstance(m, dict):
                    mobile = m
            if isinstance(mobile, dict):
                carrier = mobile.get("CARRIER")
                signal = mobile.get("SIGNAL")
                cre = mobile.get("CRE")
                if carrier not in (None, "", "NA"):
                    summary_parts.append(f"Operatore: {carrier}")
                if signal not in (None, "", "NA"):
                    pct = _signal_to_percent(signal)
                    if pct is not None:
                        summary_parts.append(f"Segnale: {signal} ({pct}%)")
                    else:
                        summary_parts.append(f"Segnale: {signal}")
                if cre not in (None, "", "NA", "4294967295"):
                    summary_parts.append(f"Credito: {cre}")
        elif str(entity_type).lower() == "thermostats":
            temp = realtime.get("TEMP") if isinstance(realtime, dict) else None
            rh = realtime.get("RH") if isinstance(realtime, dict) else None
            therm = realtime.get("THERM") if isinstance(realtime, dict) else None
            if not isinstance(therm, dict):
                therm = {}
            mode = therm.get("ACT_MODEL")
            season = therm.get("ACT_SEA")
            thr = therm.get("TEMP_THR") if isinstance(therm, dict) else None
            target = None
            if isinstance(thr, dict):
                target = thr.get("VAL")

            if temp not in (None, "", "NA"):
                summary_parts.append(f"Temp: {temp}°C")
            if rh not in (None, "", "NA"):
                summary_parts.append(f"Umidità: {rh}%")
            if season not in (None, "", "NA"):
                summary_parts.append(f"Stagione: {season}")
            if mode not in (None, "", "NA"):
                summary_parts.append(f"Modalità: {mode}")
            if target not in (None, "", "NA"):
                summary_parts.append(f"Setpoint: {target}°C")
        elif str(entity_type).lower() == "gsm":
            carrier = realtime.get("CARRIER") if isinstance(realtime, dict) else None
            signal = realtime.get("SIGNAL") if isinstance(realtime, dict) else None
            sim = realtime.get("SSIM") if isinstance(realtime, dict) else None
            last_err = realtime.get("LASTERR") if isinstance(realtime, dict) else None
            pct = realtime.get("SIGNAL_PCT") if isinstance(realtime, dict) else None

            if carrier not in (None, "", "NA"):
                summary_parts.append(f"Operatore: {carrier}")
            if signal not in (None, "", "NA"):
                if pct not in (None, "", "NA"):
                    summary_parts.append(f"Segnale: {signal} ({pct}%)")
                else:
                    p = _signal_to_percent(signal)
                    summary_parts.append(f"Segnale: {signal} ({p}%)" if p is not None else f"Segnale: {signal}")
            if sim not in (None, "", "NA"):
                summary_parts.append(f"SIM: {sim}")
            if last_err not in (None, "", "NA"):
                summary_parts.append(f"Err: {last_err}")
        elif str(entity_type).lower() == "scenarios":
            cat_s = static.get("CAT") if isinstance(static, dict) else None
            pin_s = static.get("PIN") if isinstance(static, dict) else None
            cat_u = str(cat_s or "").upper()
            if cat_u in ("ARM", "DISARM", "PARTIAL"):
                sub = {
                    "ARM": "Arm totale",
                    "DISARM": "Disarm",
                    "PARTIAL": "Parziale",
                }.get(cat_u, "Sicurezza")
                summary_parts.append(f"Gruppo: Sicurezza · {sub} ({cat_u})")
            else:
                if cat_u:
                    summary_parts.append(f"Gruppo: Smarthome ({cat_u})")
                else:
                    summary_parts.append("Gruppo: Smarthome")
            if pin_s not in (None, "", "NA"):
                summary_parts.append(f"PIN: {pin_s}")
        else:
            if sta not in (None, ""):
                summary_parts.append(f"STA={sta}")
            if pos not in (None, ""):
                summary_parts.append(f"POS={pos}")
            if arm not in (None, ""):
                summary_parts.append(f"ARM={arm}")
            if ast not in (None, ""):
                summary_parts.append(f"AST={ast}")
        summary = " | ".join(summary_parts)

        controls = "<span class=\"muted\">-</span>"
        if access == "rw":
            if entity_type == "outputs":
                cat_u = str(cat).upper()
                controls_parts = []
                if cat_u == "ROLL":
                    controls_parts.extend(
                        [
                            f"<button class=\"cmd\" onclick=\"sendCmd('outputs',{_html_escape(entity_id)},'up')\">UP</button>",
                            f"<button class=\"cmd\" onclick=\"sendCmd('outputs',{_html_escape(entity_id)},'down')\">DOWN</button>",
                            f"<button class=\"cmd\" onclick=\"sendCmd('outputs',{_html_escape(entity_id)},'stop')\">STOP</button>",
                            "<span class=\"ctl\">Pos "
                            f"<input class=\"rng\" data-key=\"outputs:{_html_escape(entity_id)}\" data-kind=\"pos\" "
                            f"type=\"range\" min=\"0\" max=\"100\" value=\"0\" "
                            f"oninput=\"debouncedCmd('outputs',{_html_escape(entity_id)},'pos',this.value,this.nextElementSibling)\"/>"
                            "<span class=\"muted val\">0</span></span>",
                        ]
                    )
                else:
                    controls_parts.extend(
                        [
                            f"<button class=\"cmd\" onclick=\"sendCmd('outputs',{_html_escape(entity_id)},'on')\">ON</button>",
                            f"<button class=\"cmd\" onclick=\"sendCmd('outputs',{_html_escape(entity_id)},'off')\">OFF</button>",
                            f"<button class=\"cmd\" onclick=\"sendCmd('outputs',{_html_escape(entity_id)},'toggle')\">TOGGLE</button>",
                        ]
                    )
                    if cat_u == "LIGHT":
                        controls_parts.append(
                            "<span class=\"ctl\">Dim "
                            f"<input class=\"rng\" data-key=\"outputs:{_html_escape(entity_id)}\" data-kind=\"dim\" "
                            f"type=\"range\" min=\"0\" max=\"100\" value=\"0\" "
                            f"oninput=\"debouncedCmd('outputs',{_html_escape(entity_id)},'brightness',this.value,this.nextElementSibling)\"/>"
                            "<span class=\"muted val\">0</span></span>"
                        )
                controls = " ".join(controls_parts)
            elif entity_type == "scenarios":
                controls = f"<button class=\"cmd\" onclick=\"sendCmd('scenarios',{_html_escape(entity_id)},'execute')\">RUN</button>"
            elif entity_type == "partitions":
                controls = (
                    f"<button class=\"cmd\" onclick=\"sendCmd('partitions',{_html_escape(entity_id)},'arm')\">ARM</button> "
                    f"<button class=\"cmd\" onclick=\"sendCmd('partitions',{_html_escape(entity_id)},'arm_instant')\">ARM INST</button> "
                    f"<button class=\"cmd\" onclick=\"sendCmd('partitions',{_html_escape(entity_id)},'disarm')\">DISARM</button>"
                )
            elif entity_type == "zones":
                controls = (
                    f"<button class=\"cmd\" onclick=\"sendCmd('zones',{_html_escape(entity_id)},'bypass_on')\">BYP ON</button> "
                    f"<button class=\"cmd\" onclick=\"sendCmd('zones',{_html_escape(entity_id)},'bypass_off')\">BYP OFF</button> "
                    f"<button class=\"cmd\" onclick=\"sendCmd('zones',{_html_escape(entity_id)},'bypass_toggle')\">BYP TGL</button>"
                )
            elif entity_type == "accounts":
                validity = static.get("VALIDITY") if isinstance(static, dict) else None
                if not isinstance(validity, dict):
                    validity = {}
                always = str(validity.get("ALWAYS") or "T").strip().upper()
                enabled = always != "F"
                if enabled:
                    controls = f"<button class=\"cmd\" onclick=\"sendCmd('accounts',{_html_escape(entity_id)},'disable')\">DISABLE</button>"
                else:
                    controls = f"<button class=\"cmd\" onclick=\"sendCmd('accounts',{_html_escape(entity_id)},'enable')\">ENABLE</button>"
            elif entity_type == "thermostats":
                k = f"thermostats:{_html_escape(entity_id)}"
                therm_rt = realtime.get("THERM") if isinstance(realtime, dict) else None
                if not isinstance(therm_rt, dict):
                    therm_rt = {}
                cur_mode = str(therm_rt.get("ACT_MODEL") or therm_rt.get("ACT_MODE") or "").upper()
                cur_sea = str(therm_rt.get("ACT_SEA") or "").upper()
                thr = therm_rt.get("TEMP_THR")
                cur_target = None
                if isinstance(thr, dict):
                    cur_target = thr.get("VAL")
                if cur_mode not in ("OFF", "MAN", "AUTO"):
                    # Some panels report other values (e.g. WEEKLY/SD1/SD2) only in CFG; keep as-is if present there.
                    try:
                        st_mode = str(static.get("ACT_MODE") or "").upper() if isinstance(static, dict) else ""
                    except Exception:
                        st_mode = ""
                    cur_mode = st_mode or cur_mode or "OFF"
                if cur_sea not in ("WIN", "SUM"):
                    cur_sea = "WIN"
                try:
                    cur_target_num = float(str(cur_target).replace(",", "."))
                except Exception:
                    cur_target_num = 20.0
                cur_target_num = max(5.0, min(35.0, cur_target_num))
                cur_target_str = f"{cur_target_num:.1f}"

                season_cfg = static.get(cur_sea) if isinstance(static, dict) else None
                if not isinstance(season_cfg, dict):
                    season_cfg = {}
                t1 = season_cfg.get("T1")
                t2 = season_cfg.get("T2")
                t3 = season_cfg.get("T3")
                try:
                    t1s = f"{float(str(t1).replace(',', '.')):.1f}" if t1 not in (None, '', 'NA') else ""
                except Exception:
                    t1s = ""
                try:
                    t2s = f"{float(str(t2).replace(',', '.')):.1f}" if t2 not in (None, '', 'NA') else ""
                except Exception:
                    t2s = ""
                try:
                    t3s = f"{float(str(t3).replace(',', '.')):.1f}" if t3 not in (None, '', 'NA') else ""
                except Exception:
                    t3s = ""
                mode_opts_html = "".join(
                    [
                        f"<option value=\"{_html_escape(m)}\" {'selected' if cur_mode == m else ''}>{_html_escape('AUTO' if m == 'WEEKLY' else m)}</option>"
                        for m in _thermostat_mode_options(static, cur_mode)
                    ]
                )
                manhrs_style = ""
                try:
                    if str(cur_mode or "").upper() != "MAN_TMR":
                        manhrs_style = "display:none"
                except Exception:
                    manhrs_style = "display:none"
                controls = (
                    f"<span class=\"ctl\">Modo <select data-key=\"{k}\" data-kind=\"mode\" onchange=\"onThermModeChanged('{k}',{_html_escape(entity_id)},this.value)\">{mode_opts_html}</select>"
                    f" <span class=\"mono\" data-key=\"{k}\" data-kind=\"manhrs-wrap\" style=\"{manhrs_style}\">"
                    f"MAN_HRS <input class=\"mono\" data-key=\"{k}\" data-kind=\"manhrs\" style=\"width:56px\" type=\"number\" min=\"0\" max=\"72\" step=\"0.5\" value=\"{_html_escape(str(static.get('MAN_HRS') or '')) if isinstance(static, dict) else ''}\" "
                    f"onchange=\"setThermManualTimer({_html_escape(entity_id)},this.value)\"/>h</span></span> "
                    "<span class=\"ctl\">Stagione "
                    f"<select data-key=\"{k}\" data-kind=\"sea\" onchange=\"sendCmd('thermostats',{_html_escape(entity_id)},'set_season',this.value)\">"
                    f"<option value=\"WIN\" {'selected' if cur_sea == 'WIN' else ''}>WIN</option>"
                    f"<option value=\"SUM\" {'selected' if cur_sea == 'SUM' else ''}>SUM</option>"
                    "</select></span> "
                    "<span class=\"ctl\">Set "
                    f"<input class=\"rng\" data-key=\"{k}\" data-kind=\"tmp\" type=\"range\" min=\"5\" max=\"35\" step=\"0.5\" value=\"{cur_target_str}\" "
                    f"oninput=\"debouncedCmd('thermostats',{_html_escape(entity_id)},'set_target',this.value,this.nextElementSibling)\"/>"
                    f"<span class=\"muted val\">{cur_target_str}</span></span>"
                    f"<span class=\"ctl\">T1 <input class=\"mono\" data-key=\"{k}\" data-kind=\"t1\" style=\"width:56px\" type=\"number\" step=\"0.5\" value=\"{_html_escape(t1s)}\" "
                    f"onchange=\"setThermProfile({_html_escape(entity_id)},'T1',this.value)\"/></span>"
                    f"<span class=\"ctl\">T2 <input class=\"mono\" data-key=\"{k}\" data-kind=\"t2\" style=\"width:56px\" type=\"number\" step=\"0.5\" value=\"{_html_escape(t2s)}\" "
                    f"onchange=\"setThermProfile({_html_escape(entity_id)},'T2',this.value)\"/></span>"
                    f"<span class=\"ctl\">T3 <input class=\"mono\" data-key=\"{k}\" data-kind=\"t3\" style=\"width:56px\" type=\"number\" step=\"0.5\" value=\"{_html_escape(t3s)}\" "
                    f"onchange=\"setThermProfile({_html_escape(entity_id)},'T3',this.value)\"/></span>"
                )

        if entity_type in ("outputs", "scenarios", "zones", "partitions"):
            fav_btn = (
                f"<button class=\"cmd fav\" data-key=\"{_html_escape(entity_type)}:{_html_escape(entity_id)}\" "
                f"onclick=\"toggleFav('{_html_escape(entity_type)}',{_html_escape(entity_id)},event)\">☆</button>"
            )
            controls = fav_btn + " " + controls

        et_lower = str(entity_type).lower()
        if et_lower in ("outputs", "scenarios"):
            if et_lower == "outputs":
                # For outputs, prefer a dropdown populated from /api/ui_tags tag_styles.
                tag_cell = (
                    f"<select class=\"tagSelect\" data-tag-type=\"{_html_escape(et_lower)}\" data-tag-id=\"{_html_escape(entity_id)}\">"
                    f"<option value=\"{_html_escape(tag_value)}\" selected>{_html_escape(tag_value) if tag_value else '-'}</option>"
                    "</select>"
                )
            else:
                tag_cell = (
                    f"<input class=\"tagInput\" data-tag-type=\"{_html_escape(et_lower)}\" "
                    f"data-tag-id=\"{_html_escape(entity_id)}\" value=\"{_html_escape(tag_value)}\" placeholder=\"Tag\"/>"
                )
            visible_cell = (
                f"<input class=\"tagVisible\" type=\"checkbox\" data-tag-type=\"{_html_escape(et_lower)}\" "
                f"data-tag-id=\"{_html_escape(entity_id)}\"{' checked' if visible_flag else ''}/>"
            )
        else:
            tag_cell = "<span class=\"muted\">-</span>"
            visible_cell = "<span class=\"muted\">-</span>"

        row_key = f"{str(entity_type).lower()}:{entity_id}"
        last_seen_ts = e.get("last_seen") or ""
        search_base = " ".join(
            [
                str(entity_type or ""),
                str(entity_id or ""),
                str(name or ""),
                str(cat or ""),
                str(access or ""),
                str(summary or ""),
            ]
        )
        search = (search_base + " " + str(tag_value or "")).lower()
        static_html = _render_kv_table(
            (
                {**static, "ZONES": zones_by_partition.get(int(entity_id), [])}
                if str(entity_type).lower() == "partitions" and str(entity_id).isdigit()
                else static
            ),
            entity_type=str(entity_type).lower(),
            kind="static",
            partition_names=partition_names,
        )
        realtime_pre = _html_escape(json.dumps(realtime, ensure_ascii=False, indent=2)) if realtime else "-"
        state_class = _row_state_class(entity_type, static, realtime)
        row_class_attr = f"main-row {state_class}".strip()

        rows.append(
            f"<tr class=\"{_html_escape(row_class_attr)}\" data-type=\"{_html_escape(str(entity_type).lower())}\" data-key=\"{_html_escape(row_key)}\" "
            f"data-lastseen=\"{_html_escape(last_seen_ts)}\" data-search=\"{_html_escape(search)}\" data-search-base=\"{_html_escape(search_base)}\" "
            f"data-tag=\"{_html_escape(tag_value)}\">"
            f"<td class=\"col-type\">{_html_escape(entity_type)}</td>"
            f"<td class=\"col-id\">{_html_escape(entity_id)}</td>"
            f"<td class=\"col-name\">{_html_escape(name)}</td>"
            f"<td class=\"col-cat\">{_html_escape(cat)}</td>"
            f"<td class=\"col-tag\">{tag_cell}</td>"
            f"<td class=\"col-visible\">{visible_cell}</td>"
            f"<td class=\"col-access\">{_html_escape(access)}</td>"
            f"<td class=\"col-summary\">{_html_escape(summary)}</td>"
            f"<td class=\"col-lastseen\">{_html_escape(_fmt_ts(e.get('last_seen')))}</td>"
            f"<td class=\"col-controls\">{controls}</td>"
            "</tr>"
            f"<tr class=\"detail-row\" data-parent=\"{_html_escape(row_key)}\">"
            f"<td colspan=\"10\">"
            f"<div class=\"detail-grid\">"
            f"<div class=\"detail-card\"><div class=\"detail-title\">Static</div>{static_html}</div>"
            f"<div class=\"detail-card\"><div class=\"detail-title\">Realtime</div><pre class=\"json live\" data-live=\"{_html_escape(row_key)}\">{realtime_pre}</pre></div>"
            f"</div>"
            f"</td>"
            "</tr>"
        )

    tbody_html = "".join(rows)

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Debug</title>
    <style>
      :root {{
        --bg: #0f1115;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: #2a2f3a;
        --hover: #171a21;
        --badge-bg: #1f2430;
        --th-bg: #0f1115;
        --input-bg: #0f1115;
        --input-fg: #e7eaf0;
      }}
      body {{
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        margin: 16px;
        background: var(--bg);
        color: var(--fg);
      }}
      body::after {{
        content: "";
        position: fixed;
        top: 12px;
        right: 12px;
        width: 180px;
        height: 80px;
        background: url('/assets/e-safe_scr.png') no-repeat center center / contain;
        opacity: 0.75;
        pointer-events: none;
      }}
      .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 12px; }}
      a, summary {{ color: var(--fg); }}
      input {{
        padding: 8px;
        width: 420px;
        max-width: 100%;
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
      }}
      input.tagInput {{
        width: 140px;
        padding: 4px 6px;
        font-size: 12px;
      }}
      input.tagVisible {{
        width: auto;
        padding: 0;
      }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid var(--border); padding: 8px; vertical-align: top; }}
      th {{ text-align: left; position: sticky; top: 0; background: var(--th-bg); }}
      tr:hover {{ background: var(--hover); }}
      tr.main-row.st-light-on {{ background: rgba(255, 193, 7, 0.16); }}
      tr.main-row.st-roll-open {{ background: rgba(76, 175, 80, 0.14); }}
      tr.main-row.st-zone-rest {{ background: rgba(76, 175, 80, 0.12); }}
      tr.main-row.st-zone-alarm {{ background: rgba(244, 67, 54, 0.16); }}
      tr.main-row.st-zone-tamper {{ background: rgba(255, 193, 7, 0.16); }}
      tr.main-row.st-zone-mask {{ background: rgba(33, 150, 243, 0.16); }}
      tr.main-row.st-part-disarmed {{ background: rgba(76, 175, 80, 0.12); }}
      tr.main-row.st-part-armed {{ background: rgba(244, 67, 54, 0.16); }}
      tr.main-row.st-part-delayed {{ background: rgba(255, 193, 7, 0.16); }}
      tr.main-row.st-therm-off {{ background: rgba(158, 158, 158, 0.12); }}
      tr.main-row.st-therm-man-win {{ background: rgba(244, 67, 54, 0.16); }}
      tr.main-row.st-therm-man-sum {{ background: rgba(33, 150, 243, 0.16); }}
      tr.main-row.st-therm-mantmr-win {{ background: rgba(255, 152, 0, 0.16); }}
      tr.main-row.st-therm-mantmr-sum {{ background: rgba(0, 188, 212, 0.16); }}
      tr.main-row.st-therm-auto {{ background: rgba(76, 175, 80, 0.10); }}
      tr.main-row.st-therm-sd1 {{ background: rgba(156, 39, 176, 0.16); }}
      tr.main-row.st-therm-sd2 {{ background: rgba(0, 150, 136, 0.16); }}
      tr.main-row.st-therm-out-on {{ box-shadow: inset 4px 0 0 rgba(255, 255, 255, 0.35); }}
      tr.detail-row td {{ border-bottom: 0; }}
      pre {{ white-space: pre-wrap; word-break: break-word; color: var(--fg); }}
      .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background: var(--badge-bg); }}
      .muted {{ color: var(--muted); }}
      table.kv {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
      table.kv td {{ border-bottom: 1px dashed var(--border); padding: 4px 6px; }}
      table.kv td.k {{ width: 140px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
      table.kv td.v {{ width: 260px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
      table.kv td.i {{ color: var(--text); }}
      input.rng {{ width: 140px; vertical-align: middle; }}
      span.val {{ display: inline-block; min-width: 2.5em; text-align: right; margin-left: 6px; }}
      pre.json {{ margin: 0; max-height: 180px; overflow: auto; white-space: pre; }}
      .toolbar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }}
      label {{ display: inline-flex; gap: 6px; align-items: center; }}
      ol.type-list {{ margin: 0; padding-left: 18px; max-width: 520px; }}
      ol.type-list li {{ margin: 2px 0; }}
      button {{
        background: var(--badge-bg);
        color: var(--fg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 6px 10px;
        cursor: pointer;
      }}
      button:hover {{ background: var(--hover); }}
      .small {{ font-size: 12px; color: var(--muted); }}
      .cmd {{ padding: 4px 8px; border-radius: 8px; }}
      .cmd.fav {{ font-size: 16px; padding: 4px 8px; }}
      .cmd.fav.on {{ color: #ffd24a; border-color: rgba(255,210,74,0.45); }}
      .cleanupBtn {{ border: 1px solid #ff4d4f; background: linear-gradient(145deg, #b00020, #ff4d4f); color: #fff; box-shadow: 0 4px 14px rgba(255,77,79,0.35); }}
      .ctl {{ margin-left: 6px; }}
      .num {{
        width: 90px;
        padding: 4px 6px;
        margin-left: 4px;
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
      }}
      #status {{ margin: 10px 0; font-size: 12px; color: var(--muted); }}
      #tableWrap {{
        width: 100%;
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: 12px;
        -webkit-overflow-scrolling: touch;
      }}
      .detail-grid {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
        padding: 10px 0;
      }}
      .detail-card {{
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 10px;
        background: rgba(255,255,255,0.02);
      }}
      .detail-title {{
        font-size: 12px;
        color: var(--muted);
        margin-bottom: 8px;
      }}
      .helpBox {{
        display: none;
        margin: 10px 0 0;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(255,255,255,0.03);
        color: rgba(255,255,255,0.86);
        font-size: 13px;
        line-height: 1.35;
      }}
      .helpBox.show {{ display: block; }}
      .helpBox code {{
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        font-size: 12px;
        padding: 1px 4px;
        border-radius: 6px;
        background: rgba(0,0,0,0.25);
        border: 1px solid rgba(255,255,255,0.08);
      }}
    </style>
  </head>
  <body>
    <h2>Ksenia Lares - index_debug {f'<span class="badge">v{_html_escape(ADDON_VERSION)}</span>' if ADDON_VERSION else ''}</h2>
    <div class="meta">
      Entità: <span class="badge">{len(entities)}</span>
      &nbsp;|&nbsp; Last update: <span id="lastUpdate" class="badge">{_html_escape(_fmt_ts(meta.get("last_update")))}</span>
      &nbsp;|&nbsp; UI: <span class="badge">{_html_escape(UI_REV)}</span>
    </div>
    <div class="toolbar">
      <input id="q" placeholder="Filtra (type/id/nome/cat/tag/access)..." oninput="filterRows()"/>
      <button id="toggle">Auto-refresh: ON</button>
      <button class="cmd" id="pinHelpBtn" type="button">PIN/WS2?</button>
      <a class="cmd" href="/logs">Registro Eventi</a>
      <a class="cmd" href="/timers">Programmatori Orari</a>
      <a class="cmd" href="/thermostats">Termostati</a>
      <a class="cmd" href="/security/users">Gestione Utenti</a>
      <a class="cmd" href="/index_debug/tag_styles">Tag: icone & colori</a>
      <button class="cmd" onclick="sendCmd('system', 1, 'clear_cycles_or_memories')">Reset cicli/memorie</button>
      <button class="cmd" onclick="sendCmd('system', 1, 'clear_communications')">Reset comunicazioni</button>
      <button class="cmd" onclick="sendCmd('system', 1, 'clear_faults_memory')">Reset guasti</button>
      <button class="cmd cleanupBtn" id="cleanupBtn" type="button" title="Pulisci config MQTT">⚠ MQTT</button>
      <button class="cmd cleanupBtn" id="republishBtn" type="button" title="Rispedisci discovery MQTT">↻ MQTT cfg</button>
      <button onclick="selectAllTypes(true)">Tutti</button>
      <button onclick="selectAllTypes(false)">Nessuno</button>
      <span class="small">Aggiorna senza perdere lo scroll</span>
    </div>
    <div class="helpBox" id="pinHelp">
      <div><b>Comandi sicurezza (WS2 + PIN)</b></div>
      <div style="margin-top:6px;">
        Scenari, Partizioni e Bypass zone usano la WS2 (sessione PIN). Se la sessione è scaduta/assente, ti chiede il PIN; se è attiva, esegue subito.
      </div>
      <div style="margin-top:6px;">
        Impostazioni in Home Assistant → Add-on → Ksenia Lares → Configurazione:
        <br/>- <code>web_pin_session_minutes_default</code>: durata token PIN (minuti)
        <br/>- <code>security_cmd_ws_idle_timeout_sec</code>: chiusura WS2 per inattività (secondi)
      </div>
    </div>
    <ol class="type-list" id="types">
      {''.join([f'<li><label><input type="checkbox" class="typeFilter" value="{_html_escape(t)}" checked onchange="applyTypeFilter()"/> {_html_escape(t)} <span class="badge">{counts.get(t,0)}</span></label></li>' for t in type_order])}
      <li><label><input id="showDetails" type="checkbox" checked onchange="applyDetails()"/> dettagli</label></li>
    </ol>
    <div id="status">Comandi: solo per righe con Access = rw</div>
    <div id="tableWrap">
      <table id="t">
        <thead>
          <tr>
            <th>Type</th>
            <th>ID</th>
            <th>Name</th>
            <th>Cat/Typ</th>
            <th>Tag</th>
            <th>Visibile</th>
            <th>Access</th>
            <th>Summary</th>
            <th>Last seen</th>
            <th>Controls</th>
          </tr>
        </thead>
        <tbody>
          {tbody_html}
        </tbody>
      </table>
    </div>
    <script>
      let pollingOn = false;
      let timer = null;
      let lastSeenUpdate = "{_html_escape(_fmt_ts(meta.get("last_update")))}";
      let allowedTypes = new Set();
      let mainRows = [];
      let rowByKey = new Map(); // key -> row info
      let detailByParent = new Map(); // parentKey -> detailRow
      let preByKey = new Map(); // key -> pre.json.live
      let filterTimer = null;
      let sse = null;
      let countdowns = new Map(); // key -> countdown info
      let countdownTimer = null;
      let debounceTimers = new Map(); // key -> timeout id
      let slidersByKey = new Map(); // key -> slider elements
      let staticTableByParent = new Map(); // parentKey -> table.kv (static)
      let staticCellsByParent = new Map(); // parentKey -> Map(fieldKey -> td.v)
      let lastThermModeChange = new Map(); // key -> ms timestamp
      let pendingThermMode = new Map(); // key -> desired mode while waiting user input (e.g. MAN_TMR)
      let lastThermManHrsChange = new Map(); // key -> ms timestamp (avoid flicker)
      let tagSaveTimers = new Map();
      const TAG_STYLE_NAMES = {tag_style_names_json};
      let favorites = {{ outputs: {{}}, scenarios: {{}}, zones: {{}}, partitions: {{}} }};

      (function setupCleanupBtn() {{
        try {{
          const btn = document.getElementById('cleanupBtn');
          if (!btn) return;
          btn.addEventListener('click', async () => {{
            if (!confirm("Pulisci i topic MQTT Discovery (homeassistant/.../config) per questo add-on?")) return;
            const prev = btn.textContent;
            btn.disabled = true;
            btn.textContent = '...';
            try {{
              const res = await fetch('/api/cmd', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ type: 'mqtt', action: 'cleanup_discovery' }}),
              }});
              const txt = await res.text();
              let data = null;
              try {{ data = JSON.parse(txt); }} catch (_e) {{}}
              if (data && data.ok) alert("Pulizia completata (" + ((data && data.cleared) ? data.cleared : 0) + ")");
              else alert("Errore pulizia: " + ((data && data.error) || txt || "sconosciuto"));
            }} catch (e) {{
              alert("Errore pulizia: " + (e && e.message ? e.message : e));
            }} finally {{
              btn.disabled = false;
              btn.textContent = prev;
            }}
          }});
        }} catch (_e) {{}}
      }})();

      (function setupRepublishBtn() {{
        try {{
          const btn = document.getElementById('republishBtn');
          if (!btn) return;
          btn.addEventListener('click', async () => {{
            if (!confirm("Rispedisci tutte le config MQTT discovery (homeassistant/.../config)?")) return;
            const prev = btn.textContent;
            btn.disabled = true;
            btn.textContent = '...';
            try {{
              const res = await fetch('/api/cmd', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ type: 'mqtt', action: 'republish_discovery' }}),
              }});
              const txt = await res.text();
              let data = null;
              try {{ data = JSON.parse(txt); }} catch (_e) {{}}
              if (data && data.ok) {{
                const published = (data && data.published != null) ? data.published : 0;
                alert("Config MQTT rispedite. Pubblicati: " + published);
              }} else {{
                alert("Errore: " + ((data && data.error) || txt || "sconosciuto"));
              }}
            }} catch (e) {{
              alert("Errore: " + (e && e.message ? e.message : e));
            }} finally {{
              btn.disabled = false;
              btn.textContent = prev;
            }}
          }});
        }} catch (_e) {{}}
      }})();

      async function loadFavorites() {{
        try {{
          const res = await fetch('/api/ui_favorites', {{ cache: 'no-store' }});
          const data = await res.json();
          if (data && typeof data === 'object') favorites = data;
        }} catch (_e) {{}}
      }}
      function isFav(type, id) {{
        const bucket = (favorites && favorites[type] && typeof favorites[type] === 'object') ? favorites[type] : {{}};
        return !!bucket[String(id)];
      }}
      async function toggleFav(type, id, ev) {{
        try {{ if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation(); }} catch (_e) {{}}
        const fav = !isFav(type, id);
        try {{
          await fetch('/api/ui_favorites', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ type, id, fav }}),
          }});
          if (!favorites[type] || typeof favorites[type] !== 'object') favorites[type] = {{}};
          if (fav) favorites[type][String(id)] = true;
          else delete favorites[type][String(id)];
          applyFavUi();
        }} catch (e) {{
          console.error('toggleFav error', e);
        }}
      }}
      function applyFavUi() {{
        document.querySelectorAll('button.fav[data-key]').forEach((btn) => {{
          const key = String(btn.getAttribute('data-key') || '');
          const parts = key.split(':');
          const t = parts[0] || '';
          const i = parts[1] || '';
          const on = isFav(t, i);
          btn.classList.toggle('on', on);
          btn.textContent = on ? '★' : '☆';
        }});
      }}

      function buildDomIndex() {{
        mainRows = Array.from(document.querySelectorAll('#t tbody tr.main-row'));
        rowByKey = new Map();
        for (const r of mainRows) {{
          rowByKey.set(String(r.dataset.key || ''), {{
            row: r,
            summaryCell: r.querySelector('.col-summary'),
            lastCell: r.querySelector('.col-lastseen'),
          }});
        }}

        detailByParent = new Map();
        const details = document.querySelectorAll('#t tbody tr.detail-row[data-parent]');
        for (const d of details) {{
          detailByParent.set(String(d.dataset.parent || ''), d);
        }}

        staticTableByParent = new Map();
        staticCellsByParent = new Map();
        for (const [parentKey, d] of detailByParent.entries()) {{
          try {{
            const table = d.querySelector('table.kv[data-kv-kind=\"static\"]');
            if (!table) continue;
            staticTableByParent.set(parentKey, table);
            const m = new Map();
            const cells = table.querySelectorAll('td.v[data-kv-key]');
            for (const td of cells) {{
              const fk = String(td.dataset.kvKey || '');
              if (fk) m.set(fk, td);
            }}
            staticCellsByParent.set(parentKey, m);
          }} catch (_e) {{}}
        }}

        preByKey = new Map();
        const pres = document.querySelectorAll('pre.json.live[data-live]');
        for (const p of pres) {{
          preByKey.set(String(p.dataset.live || ''), p);
        }}

        slidersByKey = new Map();
        const sliders = document.querySelectorAll('input.rng[data-key][data-kind]');
        for (const s of sliders) {{
          const k = String(s.dataset.key || '');
          const kind = String(s.dataset.kind || '');
          const valSpan = s.nextElementSibling;
          const current = slidersByKey.get(k) || {{}};
          if (kind === 'dim') {{
            current.dimInput = s;
            current.dimVal = valSpan;
          }} else if (kind === 'pos') {{
            current.posInput = s;
            current.posVal = valSpan;
          }} else if (kind === 'tmp') {{
            current.tmpInput = s;
            current.tmpVal = valSpan;
          }}
          slidersByKey.set(k, current);
        }}
        bindTagInputs();
      }}

      function filterRows() {{
        const q = (document.getElementById('q').value || '').toLowerCase();
        const detailsToggle = document.getElementById('showDetails');
        const detailsOn = detailsToggle ? !!detailsToggle.checked : true;
        for (const r of mainRows) {{
          const type = String(r.dataset.type || '').toLowerCase();
          const okType = allowedTypes.has(type);
          const hay = String(r.dataset.search || '');
          const show = okType && hay.includes(q);
          r.style.display = show ? '' : 'none';
          const detail = detailByParent.get(String(r.dataset.key || ''));
          if (detail) detail.style.display = (show && detailsOn) ? '' : 'none';
        }}
      }}

      function scheduleFilter() {{
        if (filterTimer) return;
        filterTimer = setTimeout(() => {{
          filterTimer = null;
          filterRows();
        }}, 80);
      }}

      function debouncedCmd(type, id, action, value, valueEl=null, delayMs=250) {{
        try {{
          if (valueEl) valueEl.textContent = String(value);
        }} catch (_e) {{}}
        const k = `${{String(type)}}:${{String(id)}}:${{String(action)}}`;
        const prev = debounceTimers.get(k);
        if (prev) clearTimeout(prev);
        const t = setTimeout(() => {{
          debounceTimers.delete(k);
          sendCmd(type, id, action, value);
        }}, delayMs);
        debounceTimers.set(k, t);
      }}

      function updateRowTagSearch(type, id, tagValue) {{
        const key = `${{String(type || '').toLowerCase()}}:${{String(id || '')}}`;
        const rowInfo = rowByKey.get(key);
        if (!rowInfo || !rowInfo.row) return;
        const row = rowInfo.row;
        row.dataset.tag = String(tagValue || '');
        const base = String(row.dataset.searchBase || '');
        row.dataset.search = (base + ' ' + String(tagValue || '')).trim().toLowerCase();
      }}

      function scheduleTagSave(el) {{
        const type = String(el.dataset.tagType || '').toLowerCase();
        const id = String(el.dataset.tagId || '');
        if (!type || !id) return;
        const key = `${{type}}:${{id}}`;
        const prev = tagSaveTimers.get(key);
        if (prev) clearTimeout(prev);
        const t = setTimeout(() => {{
          tagSaveTimers.delete(key);
          saveTag(type, id);
        }}, 500);
        tagSaveTimers.set(key, t);
      }}

      function saveTag(type, id) {{
        const tagInput = document.querySelector(`input.tagInput[data-tag-type="${{type}}"][data-tag-id="${{id}}"]`);
        const tagSelect = document.querySelector(`select.tagSelect[data-tag-type="${{type}}"][data-tag-id="${{id}}"]`);
        const visibleInput = document.querySelector(`input.tagVisible[data-tag-type="${{type}}"][data-tag-id="${{id}}"]`);
        const tag = tagSelect ? String(tagSelect.value || '').trim() : (tagInput ? String(tagInput.value || '').trim() : '');
        const visible = visibleInput ? !!visibleInput.checked : true;
        updateRowTagSearch(type, id, tag);
        sendCmd('ui_tags', id, 'set', {{ target_type: type, tag: tag, visible: visible }});
      }}

      function bindTagInputs() {{
        const tagInputs = document.querySelectorAll('input.tagInput[data-tag-type][data-tag-id]');
        for (const inp of tagInputs) {{
          if (inp.dataset.tagBound) continue;
          inp.dataset.tagBound = '1';
          inp.addEventListener('input', () => scheduleTagSave(inp));
          inp.addEventListener('change', () => scheduleTagSave(inp));
        }}
        const tagSelects = document.querySelectorAll('select.tagSelect[data-tag-type][data-tag-id]');
        for (const sel of tagSelects) {{
          if (sel.dataset.tagBound) continue;
          sel.dataset.tagBound = '1';
          sel.addEventListener('change', () => scheduleTagSave(sel));
        }}
        const visInputs = document.querySelectorAll('input.tagVisible[data-tag-type][data-tag-id]');
        for (const chk of visInputs) {{
          if (chk.dataset.tagBound) continue;
          chk.dataset.tagBound = '1';
          chk.addEventListener('change', () => scheduleTagSave(chk));
        }}
      }}

      function applyTagSelectOptions(options) {{
        const opts = Array.isArray(options) ? options.filter(Boolean) : [];
        const all = [''].concat(opts);
        const escOpt = (s) => String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
        for (const sel of document.querySelectorAll('select.tagSelect[data-tag-type][data-tag-id]')) {{
          const cur = String(sel.value || '').trim();
          sel.innerHTML = all.map(t => `<option value="${{escOpt(t)}}">${{t ? escOpt(t) : '-'}}</option>`).join('');
          sel.value = cur;
          if (sel.value !== cur) sel.value = '';
        }}
      }}

      async function refreshTagSelectOptions() {{
        // Always keep a fallback list so the dropdown works even if fetch fails (Ingress / cache / transient errors).
        let options = Array.isArray(TAG_STYLE_NAMES) ? TAG_STYLE_NAMES.slice() : [];
        try {{
          const candidates = ['api/ui_tags','./api/ui_tags','../api/ui_tags','/api/ui_tags'];
          for (const url of candidates) {{
            try {{
              const res = await fetch(url, {{ cache: 'no-store' }});
              if (!res.ok) continue;
              const tags = await res.json();
              const styles = (tags && tags.tag_styles && typeof tags.tag_styles === 'object') ? tags.tag_styles : {{}};
              const fromApi = Object.keys(styles || {{}})
                .filter(Boolean)
                .sort((a,b) => a.localeCompare(b, 'it', {{ sensitivity:'base' }}));
              if (fromApi && fromApi.length) options = fromApi;
              break;
            }} catch (_e) {{}}
          }}
        }} catch (_e) {{}}
        applyTagSelectOptions(options);
      }}

      function startCountdownTimer() {{
        if (countdownTimer) return;
        countdownTimer = setInterval(() => {{
          if (countdowns.size === 0) {{
            clearInterval(countdownTimer);
            countdownTimer = null;
            return;
          }}
          const now = Date.now() / 1000;
          for (const [key, cd] of countdowns.entries()) {{
            const elapsed = Math.max(0, now - (cd.startedAt || now));
            const remaining = Math.max(0, Math.floor((cd.baseSec || 0) - elapsed));
            const timeStr = cd.fmt(remaining);
            if (cd.rowInfo && cd.rowInfo.summaryCell) {{
              cd.rowInfo.summaryCell.textContent = (cd.prefix ? (cd.prefix + ' | ') : '') + cd.label + ': ' + timeStr;
            }}
            if (cd.rowInfo && cd.rowInfo.row) {{
              const row = cd.rowInfo.row;
              const base = String(row.dataset.searchBase || '');
              const tag = String(row.dataset.tag || '');
              const extra = `${{cd.prefix ? (cd.prefix + ' | ') : ''}}${{cd.label}}: ${{timeStr}}`;
              row.dataset.search = `${{base}} ${{tag}} ${{extra}}`.trim().toLowerCase();
            }}
            if (remaining <= 0) {{
              countdowns.delete(key);
            }}
          }}
        }}, 1000);
      }}

      function updateRowStateClass(row, entityType, rt, st) {{
        try {{
          row.classList.remove(
            'st-light-on','st-roll-open',
            'st-zone-rest','st-zone-alarm','st-zone-tamper','st-zone-mask',
            'st-part-disarmed','st-part-armed','st-part-delayed',
            'st-therm-off','st-therm-man-win','st-therm-man-sum','st-therm-mantmr-win','st-therm-mantmr-sum','st-therm-auto','st-therm-sd1','st-therm-sd2','st-therm-out-on'
          );
          const t = String(entityType || '').toLowerCase();
          if (t === 'outputs') {{
            const cat = String((st && (st.CAT || st.TYP)) || '').toUpperCase();
            const sta = String((rt && rt.STA) || '').toUpperCase();
            if (cat === 'ROLL') {{
              const rawPos = (rt && rt.POS !== undefined) ? rt.POS : null;
              const n = Number(rawPos);
              if (Number.isFinite(n)) {{
                const pct = (n > 100) ? Math.round(Math.max(0, Math.min(255, n)) / 255 * 100) : Math.round(Math.max(0, Math.min(100, n)));
                if (pct > 0) row.classList.add('st-roll-open');
              }}
              if (sta === 'UP' || sta === 'DOWN') row.classList.add('st-roll-open');
            }} else {{
              if (sta === 'ON') row.classList.add('st-light-on');
            }}
          }} else if (t === 'zones') {{
            const sta = String((rt && rt.STA) || '').toUpperCase();
            if (sta === 'R') row.classList.add('st-zone-rest');
            else if (sta === 'A') row.classList.add('st-zone-alarm');
            else if (sta === 'T') row.classList.add('st-zone-tamper');
            else if (sta === 'FM') row.classList.add('st-zone-mask');
          }} else if (t === 'partitions') {{
            const arm = String((rt && rt.ARM) || '').toUpperCase();
            if (arm === 'D') row.classList.add('st-part-disarmed');
            else if (arm === 'IA') row.classList.add('st-part-armed');
            else if (arm === 'DA' || arm === 'OT' || arm === 'IT') row.classList.add('st-part-delayed');
          }} else if (t === 'thermostats') {{
            const therm = (rt && rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
            const mode = therm ? String((therm.ACT_MODEL || therm.ACT_MODE || '')).toUpperCase() : '';
            const sea = therm ? String((therm.ACT_SEA || '')).toUpperCase() : '';
            const out = therm ? String(therm.OUT_STATUS || '').toUpperCase() : '';
            if (mode === 'OFF') row.classList.add('st-therm-off');
            else if (mode === 'MAN') row.classList.add(sea === 'WIN' ? 'st-therm-man-win' : 'st-therm-man-sum');
            else if (mode === 'MAN_TMR') row.classList.add(sea === 'WIN' ? 'st-therm-mantmr-win' : 'st-therm-mantmr-sum');
            else if (mode === 'SD1') row.classList.add('st-therm-sd1');
            else if (mode === 'SD2') row.classList.add('st-therm-sd2');
            else if (mode === 'WEEKLY') row.classList.add('st-therm-auto');
            if (out === 'ON') row.classList.add('st-therm-out-on');
          }}
        }} catch (_e) {{}}
      }}

      function applyTypeFilter() {{
        const checks = document.querySelectorAll('.typeFilter');
        allowedTypes = new Set();
        for (const c of checks) {{
          if (c.checked) allowedTypes.add(String(c.value));
        }}
        try {{
          const arr = Array.from(checks).filter(c => c.checked).map(c => String(c.value));
          localStorage.setItem('ksenia_index_debug_types', JSON.stringify(arr));
        }} catch (_e) {{}}
        scheduleFilter();
      }}

      function loadTypePrefs() {{
        try {{
          const raw = localStorage.getItem('ksenia_index_debug_types') || '';
          if (!raw) return false;
          const arr = JSON.parse(raw);
          if (!Array.isArray(arr)) return false;
          const wanted = new Set(arr.map(x => String(x)));
          const checks = document.querySelectorAll('.typeFilter');
          for (const c of checks) {{
            const v = String(c.value);
            c.checked = wanted.has(v);
          }}
          return true;
        }} catch (_e) {{
          return false;
        }}
      }}

      function selectAllTypes(on) {{
        const checks = document.querySelectorAll('.typeFilter');
        for (const c of checks) c.checked = !!on;
        applyTypeFilter();
      }}

      function applyDetails() {{
        scheduleFilter();
      }}

      async function fetchAndUpdate() {{
        try {{
          const res = await fetch('./api/entities', {{ cache: 'no-store' }});
          if (!res.ok) return;
          const data = await res.json();
          const meta = data.meta || {{}};
          const entities = (data.entities || []);

          const lastUpdateStr = meta.last_update ? new Date(meta.last_update * 1000).toISOString().replace('T', ' ').slice(0, 19) : '-';
          document.getElementById('lastUpdate').innerText = lastUpdateStr;
          applyUpdateEntities(entities, meta);
        }} catch (e) {{
          // ignore
        }}
      }}

      function applyUpdateEntities(entities, meta) {{
        if (meta && meta.last_update) {{
          const lastUpdateStr = new Date(meta.last_update * 1000).toISOString().replace('T', ' ').slice(0, 19);
          const el = document.getElementById('lastUpdate');
          if (el) el.innerText = lastUpdateStr;
        }}

        if (!Array.isArray(entities)) return;
        for (const e of entities) {{
          const t = String(e.type || '').toLowerCase();
          const id = e.id ?? '';
          const key = `${{t}}:${{id}}`;
          const rowInfo = rowByKey.get(key);
          if (!rowInfo) continue;
          const row = rowInfo.row;

          // Any incoming update overrides any previously running client-side countdown.
          countdowns.delete(key);

          const lastSeenTs = String(e.last_seen ?? '');
          const lastSeenChanged = (row.dataset.lastseen !== lastSeenTs);
          row.dataset.lastseen = lastSeenTs;

          const rt = (e.realtime || {{}});
          const st = (e.static || {{}}); // may be empty

          function escHtml(s) {{
            return String(s)
              .replaceAll('&', '&amp;')
              .replaceAll('<', '&lt;')
              .replaceAll('>', '&gt;')
              .replaceAll('\"', '&quot;')
              .replaceAll(\"'\", '&#39;');
          }}

          function renderKvValue(v) {{
            if (v && typeof v === 'object') {{
              try {{
                return '<pre class=\"json\">' + escHtml(JSON.stringify(v, null, 2)) + '</pre>';
              }} catch (_e) {{
                return '<pre class=\"json\">-</pre>';
              }}
            }}
            if (v === null || v === undefined) return '';
            return escHtml(String(v));
          }}

          function updateStaticKv(parentKey, obj) {{
            if (!obj || typeof obj !== 'object') return;
            const table = staticTableByParent.get(parentKey);
            if (!table) return;
            let m = staticCellsByParent.get(parentKey);
            if (!m) {{
              m = new Map();
              staticCellsByParent.set(parentKey, m);
            }}
            for (const [fk, fv] of Object.entries(obj)) {{
              const kf = String(fk);
              if (t === 'thermostats' && kf === 'MAN_HRS') {{
                const last = Number(lastThermManHrsChange.get(parentKey) || 0);
                if ((Date.now() - last) < 2500) {{
                  continue;
                }}
              }}
              let td = m.get(kf);
              if (!td) {{
                // append a new row (info unknown client-side)
                const tr = document.createElement('tr');
                tr.dataset.kvKey = kf;
                const tdK = document.createElement('td');
                tdK.className = 'k';
                tdK.textContent = kf;
                const tdV = document.createElement('td');
                tdV.className = 'v';
                tdV.dataset.kvKey = kf;
                const tdI = document.createElement('td');
                tdI.className = 'i';
                tdI.innerHTML = '<span class=\"muted\">-</span>';
                tr.appendChild(tdK);
                tr.appendChild(tdV);
                tr.appendChild(tdI);
                table.appendChild(tr);
                td = tdV;
                m.set(kf, td);
              }}
              td.innerHTML = renderKvValue(fv);
            }}
          }}
          const summaryParts = [];
          if (rt && typeof rt === 'object') {{
            function parseTimeSeconds(v) {{
              if (v === null || v === undefined) return null;
              if (typeof v === 'number' && Number.isFinite(v)) return Math.max(0, Math.floor(v));
              const s = String(v).trim();
              if (!s) return null;
              if (/^\\d+$/.test(s)) return Math.max(0, parseInt(s, 10));
              const parts = s.split(':').map(p => parseInt(p, 10));
              if (parts.some(p => !Number.isFinite(p))) return null;
              if (parts.length === 2) return Math.max(0, parts[0] * 60 + parts[1]);
              if (parts.length === 3) return Math.max(0, parts[0] * 3600 + parts[1] * 60 + parts[2]);
              return null;
            }}
            function fmtSeconds(sec) {{
              sec = Math.max(0, Math.floor(sec));
              const h = Math.floor(sec / 3600);
              const m = Math.floor((sec % 3600) / 60);
              const s = sec % 60;
              const mm = String(m).padStart(2, '0');
              const ss = String(s).padStart(2, '0');
              if (h > 0) return String(h) + ':' + mm + ':' + ss;
              return String(m) + ':' + ss.padStart(2, '0');
            }}

            if (t === 'partitions') {{
              const armCode = String(rt.ARM ?? '').toUpperCase();
              const astCode = String(rt.AST ?? '').toUpperCase();
              const tstCode = String(rt.TST ?? '').toUpperCase();

              const armMap = {{
                D: 'Disinserita',
                DA: 'Inserimento ritardato (uscita)',
                IA: 'Inserita (istantanea)',
                IT: 'Ingresso in corso',
                OT: 'Uscita in corso',
              }};
              const astMap = {{
                OK: 'Nessun allarme',
                AL: 'Allarme attivo',
                AM: 'Memoria allarme',
              }};
              const tstMap = {{
                OK: 'Nessun sabotaggio',
                TAM: 'Sabotaggio in corso',
                AM: 'Memoria sabotaggio',
              }};

              if (armCode) summaryParts.push('Stato: ' + (armMap[armCode] || armCode) + ' (' + armCode + ')');
              if (astCode) summaryParts.push('Allarme: ' + (astMap[astCode] || astCode) + ' (' + astCode + ')');
              if (tstCode) summaryParts.push('Sabotaggio: ' + (tstMap[tstCode] || tstCode) + ' (' + tstCode + ')');

              const rawTime = (rt.TIME !== undefined && rt.TIME !== '') ? rt.TIME : rt.T;
              const base = parseTimeSeconds(rawTime);

              let timeKind = 'Tempo';
              if (armCode === 'IT') timeKind = 'Tempo ingresso';
              else if (armCode === 'OT' || armCode === 'DA') timeKind = 'Tempo uscita';

              if (base !== null) {{
                if (timeKind === 'Tempo ingresso' || timeKind === 'Tempo uscita') {{
                  const prefix = summaryParts.join(' | ');
                  countdowns.set(key, {{
                    label: timeKind,
                    baseSec: base,
                    startedAt: Date.now() / 1000,
                    prefix: prefix,
                    rowInfo: rowInfo,
                    type: t,
                    id: id,
                    name: (e.name || ''),
                    cat: ((e.static && (e.static.CAT || e.static.TYP)) || ''),
                    access: (e.access || ''),
                    fmt: fmtSeconds,
                  }});
                  startCountdownTimer();
                }}
                summaryParts.push(timeKind + ': ' + fmtSeconds(base));
              }} else if (rawTime !== undefined && rawTime !== '') {{
                summaryParts.push(timeKind + ': ' + String(rawTime));
              }}
            }} else if (t === 'zones') {{
              const staCode = String(rt.STA ?? '').toUpperCase();
              const bypCode = String(rt.BYP ?? '').toUpperCase();
              const tMem = rt.T;

              const staMap = {{
                R: 'Normale',
                A: 'Allarme',
                FM: 'Mascheramento/Guasto',
                T: 'Sabotaggio',
                E: 'Errore',
              }};
              const bypMap = {{
                NO: 'Non esclusa',
                UN_BYPASS: 'Non esclusa',
                AUTO: 'Esclusa (auto)',
                MAN_I: 'Esclusa (manuale ingresso)',
                MAN_M: 'Esclusa (manuale menu)',
                BYPASS: 'Esclusa',
              }};

              if (staCode) summaryParts.push('Stato: ' + (staMap[staCode] || staCode) + ' (' + staCode + ')');
              if (bypCode) summaryParts.push('Esclusione: ' + (bypMap[bypCode] || bypCode) + ' (' + bypCode + ')');
              if (tMem !== undefined && tMem !== null && String(tMem) !== '' && String(tMem) !== '0') summaryParts.push('Memoria: ' + String(tMem));
            }} else if (t === 'systems') {{
              const armObj = (rt.ARM && typeof rt.ARM === 'object') ? rt.ARM : ((e.static && e.static.ARM && typeof e.static.ARM === 'object') ? e.static.ARM : null);
              if (armObj) {{
                const sCode = String(armObj.S ?? '').toUpperCase();
                const dDesc = String(armObj.D ?? '').trim();
                const statusMap = {{
                  D: 'Disinserito',
                  T: 'Inserito totale',
                  T_IN: 'Inserito totale (ingresso)',
                  T_OUT: 'Inserito totale (uscita)',
                  P: 'Inserito parziale',
                  P_IN: 'Inserito parziale (ingresso)',
                  P_OUT: 'Inserito parziale (uscita)',
                }};
                const status = statusMap[sCode] || (sCode || '');
                if (status) summaryParts.push('Stato: ' + status + (sCode ? (' (' + sCode + ')') : ''));
                if (dDesc) summaryParts.push('Modalità: ' + dDesc);
              }}

              const tempObj = (rt.TEMP && typeof rt.TEMP === 'object') ? rt.TEMP : ((e.static && e.static.TEMP && typeof e.static.TEMP === 'object') ? e.static.TEMP : null);
              function fmtTemp(v) {{
                if (v === null || v === undefined) return null;
                let s = String(v).trim();
                if (!s || s.toUpperCase() === 'NA') return null;
                if (s.includes('|')) {{
                  const parts = s.split('|').map(p => p.trim()).filter(Boolean);
                  if (parts.length) s = parts[parts.length - 1];
                }}
                if (s.startsWith('+')) s = s.slice(1);
                s = s.replace(',', '.');
                const n = Number(s);
                if (!Number.isFinite(n)) return null;
                return n.toFixed(1) + '°C';
              }}
              if (tempObj) {{
                const tin = fmtTemp(tempObj.IN);
                const tout = fmtTemp(tempObj.OUT);
                if (tin) summaryParts.push('Temp interna: ' + tin);
                if (tout) summaryParts.push('Temp esterna: ' + tout);
              }}
            }} else if (t === 'domus') {{
              const src = (rt.DOMUS && typeof rt.DOMUS === 'object') ? rt.DOMUS : rt;
              function boolIt(v) {{
                const s = String(v ?? '').toUpperCase();
                if (s === 'T') return true;
                if (s === 'F') return false;
                return null;
              }}
              const tem = fmtTemp(src.TEM);
              const hum = src.HUM;
              const lht = src.LHT;
              const pir = boolIt(src.PIR);
              const tl = boolIt(src.TL);
              const th = boolIt(src.TH);
              const staDom = String(rt.STA ?? '').toUpperCase();

              if (tem) summaryParts.push('Temperatura: ' + tem);
              if (hum !== undefined && hum !== null && String(hum) !== '' && String(hum).toUpperCase() !== 'NA') summaryParts.push('Umidità: ' + String(hum) + '%');
              if (lht !== undefined && lht !== null && String(lht) !== '' && String(lht).toUpperCase() !== 'NA') summaryParts.push('Luminosità: ' + String(lht) + ' lx');
              if (pir !== null) summaryParts.push('Movimento: ' + (pir ? 'Sì' : 'No'));
              if (tl !== null) summaryParts.push('Soglia luce: ' + (tl ? 'Attiva' : 'OK'));
              if (th !== null) summaryParts.push('Soglia umidità: ' + (th ? 'Attiva' : 'OK'));
              if (staDom) summaryParts.push('Stato: ' + staDom);
            }} else if (t === 'connection') {{
              const mobile = (rt.MOBILE && typeof rt.MOBILE === 'object') ? rt.MOBILE : null;
              function sigPct(v) {{
                if (v === null || v === undefined) return null;
                const n = Number(String(v).replace(',', '.'));
                if (!Number.isFinite(n) || n < 0) return null;
                if (n <= 31) return Math.round(n / 31 * 100);
                if (n <= 100) return Math.round(n);
                return null;
              }}
              if (mobile) {{
                const carrier = mobile.CARRIER;
                const signal = mobile.SIGNAL;
                const cre = mobile.CRE;
                if (carrier !== undefined && carrier !== null && String(carrier) !== '' && String(carrier).toUpperCase() !== 'NA') summaryParts.push('Operatore: ' + String(carrier));
                if (signal !== undefined && signal !== null && String(signal) !== '' && String(signal).toUpperCase() !== 'NA') {{
                  const p = sigPct(signal);
                  summaryParts.push('Segnale: ' + String(signal) + (p !== null ? (' (' + String(p) + '%)') : ''));
                }}
                if (cre !== undefined && cre !== null && String(cre) !== '' && String(cre).toUpperCase() !== 'NA' && String(cre) !== '4294967295') summaryParts.push('Credito: ' + String(cre));
              }}
            }} else if (t === 'thermostats') {{
              const temp = rt.TEMP;
              const rh = rt.RH;
              const therm = (rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
              const mode = therm ? therm.ACT_MODEL : null;
              const season = therm ? therm.ACT_SEA : null;
              const thr = therm && therm.TEMP_THR && typeof therm.TEMP_THR === 'object' ? therm.TEMP_THR : null;
              const target = thr ? thr.VAL : null;
              if (temp !== undefined && temp !== null && String(temp) !== '' && String(temp).toUpperCase() !== 'NA') summaryParts.push('Temp: ' + String(temp) + '°C');
              if (rh !== undefined && rh !== null && String(rh) !== '' && String(rh).toUpperCase() !== 'NA') summaryParts.push('Umidità: ' + String(rh) + '%');
              if (season !== undefined && season !== null && String(season) !== '' && String(season).toUpperCase() !== 'NA') summaryParts.push('Stagione: ' + String(season));
              if (mode !== undefined && mode !== null && String(mode) !== '' && String(mode).toUpperCase() !== 'NA') summaryParts.push('Modalità: ' + String(mode));
              if (target !== undefined && target !== null && String(target) !== '' && String(target).toUpperCase() !== 'NA') summaryParts.push('Setpoint: ' + String(target) + '°C');
            }} else {{
              if (rt.STA !== undefined && rt.STA !== '') summaryParts.push('STA=' + rt.STA);
              if (rt.POS !== undefined && rt.POS !== '') summaryParts.push('POS=' + rt.POS);
              const arm = rt.ARM;
              if (arm !== undefined && arm !== '') summaryParts.push('ARM=' + arm);
              const ast = rt.AST;
              if (ast !== undefined && ast !== '') summaryParts.push('AST=' + ast);
            }}
          }}

          // Keep sliders aligned with realtime state (outputs only).
          if (t === 'outputs') {{
            const sliderKey = `outputs:${{id}}`;
            const s = slidersByKey.get(sliderKey);
            const cat = String((e.static && (e.static.CAT || e.static.TYP)) || '').toUpperCase();
            const rawPos = (rt && typeof rt === 'object') ? rt.POS : null;
            let pct = null;
            if (rawPos !== null && rawPos !== undefined && String(rawPos) !== '') {{
              const n = Number(rawPos);
              if (Number.isFinite(n)) {{
                pct = (n > 100) ? Math.round(Math.max(0, Math.min(255, n)) / 255 * 100) : Math.round(Math.max(0, Math.min(100, n)));
              }}
            }}
            if (s && pct !== null) {{
              if (cat === 'LIGHT' && s.dimInput) {{
                s.dimInput.value = String(pct);
                if (s.dimVal) s.dimVal.textContent = String(pct);
              }}
              if (cat === 'ROLL' && s.posInput) {{
                s.posInput.value = String(pct);
                if (s.posVal) s.posVal.textContent = String(pct);
              }}
            }}
          }}
          if (t === 'thermostats') {{
            const sliderKey = `thermostats:${{id}}`;
            const s = slidersByKey.get(sliderKey);
            const therm = (rt && rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
             const thr = therm && therm.TEMP_THR && typeof therm.TEMP_THR === 'object' ? therm.TEMP_THR : null;
             const target = thr ? thr.VAL : null;
             if (s && s.tmpInput && target !== null && target !== undefined && String(target) !== '') {{
               s.tmpInput.value = String(target);
               if (s.tmpVal) s.tmpVal.textContent = String(target);
             }}
             // Keep selects aligned (mode/season)
             try {{
               const mode = therm ? String(therm.ACT_MODEL || '') : '';
               const season = therm ? String(therm.ACT_SEA || '') : '';
               const selMode = row.querySelector('select[data-kind=\"mode\"][data-key=\"' + sliderKey + '\"]');
               const selSea = row.querySelector('select[data-kind=\"sea\"][data-key=\"' + sliderKey + '\"]');
               if (selMode && mode) {{
                 const pend = String(pendingThermMode.get(sliderKey) || '');
                 const last = Number(lastThermModeChange.get(sliderKey) || 0);
                 if (pend && pend.toUpperCase() === 'MAN_TMR') {{
                   // User is selecting MAN_TMR and hasn't confirmed duration yet: don't override.
                 }} else if ((Date.now() - last) > 2500 && document.activeElement !== selMode) {{
                   selMode.value = mode;
                 }}
               }}
               if (selSea && season) selSea.value = season;
               if (selMode) {{
                 // Keep MAN_HRS visibility in sync
                 try {{ onThermModeChanged(sliderKey, id, selMode.value, true); }} catch (_e) {{}}
               }}
             }} catch (_e) {{}}

             // Keep MAN_HRS input aligned with static config.
             try {{
               const selSea = row.querySelector('select[data-kind=\"sea\"][data-key=\"' + sliderKey + '\"]');
               const season = selSea ? String(selSea.value || '') : (therm ? String(therm.ACT_SEA || '') : '');
               const seaKey = (season === 'SUM' || season === 'WIN') ? season : 'WIN';
               const st = (e.static && typeof e.static === 'object') ? e.static : null;
               const seaCfg = (st && st[seaKey] && typeof st[seaKey] === 'object') ? st[seaKey] : null;
               function upd(kind, v) {{
                 const inp = row.querySelector('input[data-kind=\"' + kind + '\"][data-key=\"' + sliderKey + '\"]');
                 if (!inp) return;
                 if (document.activeElement === inp) return;
                  if (kind === 'manhrs') {{
                    const last = Number(lastThermManHrsChange.get(sliderKey) || 0);
                    if ((Date.now() - last) < 2500) return;
                  }}
                  if (v !== undefined && v !== null && String(v) !== '' && String(v).toUpperCase() !== 'NA') {{
                    inp.value = String(v);
                  }}
                }}
               if (st) {{
                 upd('manhrs', st.MAN_HRS);
               }}
               if (seaCfg) {{
                 upd('t1', seaCfg.T1);
                 upd('t2', seaCfg.T2);
                 upd('t3', seaCfg.T3);
               }}
             }} catch (_e) {{}}
           }}
          const summary = summaryParts.join(' | ');
          const last = e.last_seen ? new Date(e.last_seen * 1000).toISOString().replace('T',' ').slice(0,19) : '-';

          if (rowInfo.summaryCell) rowInfo.summaryCell.textContent = summary;
          if (rowInfo.lastCell && lastSeenChanged) rowInfo.lastCell.textContent = last;
          row.dataset.search = `${{t}} ${{id}} ${{(e.name||'')}} ${{((e.static && (e.static.CAT || e.static.TYP))||'')}} ${{(e.access||'')}} ${{summary}}`.toLowerCase();
          updateRowStateClass(row, t, rt, e.static || null);

          const pre = preByKey.get(key);
          if (pre) {{
            try {{
              pre.textContent = e.realtime ? JSON.stringify(e.realtime, null, 2) : '-';
            }} catch (_e) {{
              pre.textContent = '-';
            }}
          }}

          // Update Static key/value table (so static changes show without page reload).
          updateStaticKv(key, st);
        }}
        scheduleFilter();
      }}

      function startSSE() {{
        if (!window.EventSource) return false;
        try {{
          sse = new EventSource('./api/stream');
        }} catch (_e) {{
          return false;
        }}
        sse.onmessage = (ev) => {{
          try {{
            const msg = JSON.parse(ev.data || '{{}}');
            if (msg && msg.type === 'update') {{
              applyUpdateEntities(msg.entities || [], msg.meta || null);
              return;
            }}
            if (msg && msg.type === 'snapshot') {{
              applyUpdateEntities(msg.entities || [], msg.meta || null);
            }}
          }} catch (_e) {{
            // ignore
          }}
        }};
        sse.onerror = () => {{
          try {{ sse.close(); }} catch (_e) {{}}
          sse = null;
          // Fallback to polling if SSE fails.
          startPolling();
        }};
        return true;
      }}

      function startPolling() {{
        pollingOn = true;
        const btn = document.getElementById('toggle');
        if (btn) btn.innerText = 'Auto-refresh: ON';
        if (timer) return;
        fetchAndUpdate();
        timer = setInterval(fetchAndUpdate, 5000);
      }}

      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';

      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}

      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch('./api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) {{
          throw new Error((data && data.error) ? data.error : txt);
        }}
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}

      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares (sessione temporanea):');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}

      async function sendCmd(type, id, action, value=null, _retry=false) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) {{
          payload.value = value;
        }}
        const token = getPinSessionToken();
        if (token) payload.token = token;

        const status = document.getElementById('status');
        status.innerText = `Invio comando: ${{payload.type}}/${{payload.id}} ${{payload.action}}`;
        try {{
          const res = await fetch('./api/cmd', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
          }});
          const text = await res.text();
          let data = null;
          try {{ data = JSON.parse(text); }} catch (_e) {{}}

          const errRaw = (data && data.error !== undefined) ? String(data.error) : '';
          const err = errRaw.trim().toLowerCase();
          const needsPin = (
            err === 'pin_session_required' ||
            err === 'invalid_token' ||
            err === 'login_failed' ||
            (data && data.ok === false && err === '')
          );
          if (needsPin && !_retry) {{
            try {{
              localStorage.removeItem(PIN_TOKEN_KEY);
              localStorage.removeItem(PIN_EXP_KEY);
            }} catch (_e) {{}}
            try {{
              await ensurePinSession();
            }} catch (e) {{
              const emsg = (e && e.message) ? e.message : String(e || 'PIN non valido');
              status.innerText = `ERR: ${{emsg}}`;
              return;
            }}
            return await sendCmd(type, id, action, value, true);
          }}

          status.innerText = res.ok ? `OK: ${{text}}` : `ERR(${{res.status}}): ${{text}}`;
        }} catch (e) {{
          status.innerText = `ERR: ${{e}}`;
        }}
      }}

      function thermSeason(id) {{
        try {{
          const key = `thermostats:${{id}}`;
          const sel = document.querySelector(`select[data-key=\"${{key}}\"][data-kind=\"sea\"]`);
          const v = sel ? String(sel.value || '') : '';
          return (v === 'SUM' || v === 'WIN') ? v : 'WIN';
        }} catch (_e) {{
          return 'WIN';
        }}
      }}

      async function setThermProfile(id, levelKey, value) {{
        const season = thermSeason(id);
        const payload = {{season: season, key: String(levelKey || ''), value: value}};
        await sendCmd('thermostats', id, 'set_profile', payload);
      }}

      async function setThermManualTimer(id, rawHrs) {{
        const key = `thermostats:${id}`;
        pendingThermMode.delete(key);
        lastThermModeChange.set(key, Date.now());
        lastThermManHrsChange.set(key, Date.now());
        await sendCmd('thermostats', id, 'set_manual_timer', rawHrs);
      }}

      function onThermModeChanged(key, id, modeValue, onlyUi=false) {{
        // key can be 'thermostats:ID' or full key; normalize to string
        const k = String(key || `thermostats:${id}`);
        const desired = String(modeValue || '').toUpperCase();
        const row = rowByKey.get(k);
        if (row && row.row) {{
          const wrap = row.row.querySelector('span[data-kind=\"manhrs-wrap\"][data-key=\"' + k + '\"]');
          if (wrap) {{
            const show = desired === 'MAN_TMR';
            wrap.style.display = show ? '' : 'none';
          }}
        }}
        if (!onlyUi) {{
          // UX: MAN_TMR requires MAN_HRS; don't send the mode until the user sets the duration.
          if (desired === 'MAN_TMR') {{
            pendingThermMode.set(k, 'MAN_TMR');
            // keep UI stable
            lastThermModeChange.set(k, Date.now());
            return;
          }}
          pendingThermMode.delete(k);
          lastThermModeChange.set(k, Date.now());
          sendCmd('thermostats', id, 'set_mode', modeValue);
        }}
      }}

      function togglePolling() {{
        pollingOn = !pollingOn;
        const btn = document.getElementById('toggle');
        btn.innerText = 'Auto-refresh: ' + (pollingOn ? 'ON' : 'OFF');
        if (timer) {{
          clearInterval(timer);
          timer = null;
        }}
        if (pollingOn) {{
          fetchAndUpdate();
          timer = setInterval(fetchAndUpdate, 5000);
        }}
      }}

      // Initialize type filter from rendered checkboxes
      buildDomIndex();
      loadFavorites().then(() => applyFavUi()).catch(() => applyFavUi());
      try {{
        const btn = document.getElementById('pinHelpBtn');
        const box = document.getElementById('pinHelp');
        if (btn && box) btn.addEventListener('click', () => box.classList.toggle('show'));
      }} catch (_e) {{}}
      const hasTypePrefs = loadTypePrefs();
      if (!hasTypePrefs) {{
        // Reduce noise by default; the user can re-enable from the type list.
        try {{
          for (const c of document.querySelectorAll('.typeFilter')) {{
            if (String(c.value || '').toLowerCase() === 'memoria_allarmi') c.checked = false;
          }}
        }} catch (_e) {{}}
      }}
      applyTypeFilter();
      refreshTagSelectOptions();
      setInterval(refreshTagSelectOptions, 15000);
      // Keep a polling fallback even when SSE is connected: SSE events are deltas
      // and can be missed (addon restart, brief network hiccup, browser tab sleep).
      // Polling can still be disabled via the UI toggle if desired.
      startPolling();
      startSSE();
      // Seed one fetch to initialize countdowns even if SSE messages are sparse.
      fetchAndUpdate();
    </script>
  </body>
</html>
"""
    return html.encode("utf-8")


def render_thermostats(snapshot):
    entities = snapshot.get("entities") or []
    therms = [e for e in entities if str(e.get("type") or "").lower() == "thermostats"]
    # Note: this is the thermostat list page (no single thermostat selected).
    # These locals are referenced by the HTML template; keep them defined to avoid NameError.
    title = "Termostati"
    thermostat_id = ""

    links = []
    for e in therms:
        tid = e.get("id")
        name = e.get("name") or (e.get("static") or {}).get("DES") or f"Thermostat {tid}"
        links.append(
            f"<li><a href=\"./thermostats/{_html_escape(str(tid))}\">{_html_escape(str(name))} (ID {_html_escape(str(tid))})</a></li>"
        )
    items = "<ul>" + "".join(links) + "</ul>" if links else "<p class=\"muted\">Nessun termostato trovato</p>"

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Termostati</title>
    <style>
      :root {{
        --bg: #0f1115;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: #2a2f3a;
        --card: #151923;
      }}
      body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; color: var(--fg); background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, #05070b 60%, #000 100%); }}
      a {{ color: var(--fg); }}
      .wrap {{ max-width: 980px; margin: 24px auto; padding: 0 16px; }}
      .top {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; }}
      .badge {{ display:inline-block; padding:2px 10px; border:1px solid var(--border); border-radius: 999px; color: var(--muted); }}
      .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }}
      .muted {{ color: var(--muted); }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/thermostats" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab active" href="#temperature" id="tabTemp">Temperatura</a>
      <a class="tab" href="#humidity" id="tabHum">Umidità</a>
      <a class="tab" href="#schedule" id="tabSch">Schedulazione</a>
      <a class="tab" href="#extra" id="tabEx">Extra</a>
    </div>
    <div class="wrap">
      <div class="titleRow">
        <div>
          <div class="title">{_html_escape(title)}</div>
          <div class="meta">
            ID <span class="badge">{_html_escape(str(thermostat_id))}</span>
            · v <span class="badge">{_html_escape(ADDON_VERSION)}</span>
            · UI <span class="badge">{_html_escape(UI_REV)}</span>
            · Aggiornato: <span class="badge" id="lastUpdate">-</span>
          </div>
        </div>
        <div class="chips">
          <div class="chip"><span class="m">Stato</span> <span id="chipState">-</span></div>
          <div class="chip"><span class="m">Stagione</span> <span id="chipSeason">-</span></div>
          <div class="chip"><span class="m">Modo</span> <span id="chipMode">-</span></div>
          <div class="chip"><span class="m">Umidità</span> <span id="chipRh">-</span></div>
        </div>
      </div>

      <div class="panel">
        <div class="panelBody" id="pageTemp">
          <div class="hero">
            <div class="ringWrap">
              <svg class="ringSvg" viewBox="0 0 200 200">
                <circle cx="100" cy="100" r="84" stroke="rgba(255,255,255,0.08)" stroke-width="8" fill="none"/>
                <circle id="ringBg" cx="100" cy="100" r="84" stroke="var(--gray2)" stroke-width="10" fill="none" stroke-linecap="round"/>
                <circle id="ringFg" cx="100" cy="100" r="84" stroke="var(--gray)" stroke-width="10" fill="none" stroke-linecap="round" stroke-dasharray="0 999"/>
              </svg>
              <div class="center">
                <div class="tiny" id="centerSub">—</div>
                <div class="big" id="centerTemp">--,-</div>
                <div class="smallLine">
                  <span class="badge" id="centerTarget">Set --,-</span>
                  <span class="badge" id="centerOut">Uscita: --</span>
                </div>
              </div>
            </div>
          </div>

          <div class="btnRow">
            <button class="roundBtn" id="btnDown" type="button" aria-label="Diminuisci">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M6 15l6-6 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <button class="roundBtn" id="btnUp" type="button" aria-label="Aumenta">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
          </div>

          <div class="actionRow">
            <div class="action" id="actSeason" role="button" tabindex="0">
              <div class="ico">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M12 2c2 3-1 4 1 7s6 3 6 8a7 7 0 1 1-14 0c0-3 2-5 4-7s1-4 3-8z" stroke="currentColor" stroke-width="1.6" fill="none"/>
                </svg>
              </div>
              <div class="lab">Stagione</div>
            </div>
            <div class="action" id="actMode" role="button" tabindex="0">
              <div class="ico">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                </svg>
              </div>
              <div class="lab">Preset</div>
            </div>
            <div class="action" id="actSchedule" role="button" tabindex="0">
              <div class="ico">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <rect x="4" y="5" width="16" height="15" rx="2" stroke="currentColor" stroke-width="1.6"/>
                  <path d="M8 3v4M16 3v4M7 10h10M7 14h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
                </svg>
              </div>
              <div class="lab">Schedulazione</div>
            </div>
            <div class="action" id="actExtra" role="button" tabindex="0">
              <div class="ico">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M12 7v10M7 12h10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                  <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/>
                </svg>
              </div>
              <div class="lab">T1/T2/T3/TM</div>
            </div>
          </div>
        </div>

        <div class="panelBody" id="pageHum" style="display:none"></div>
        <div class="panelBody" id="pageSch" style="display:none"></div>
        <div class="panelBody" id="pageEx" style="display:none"></div>
      </div>
      <div class="top">
        <h2 style="margin:0">Termostati <span class="badge">UI {_html_escape(UI_REV)}</span></h2>
        <div class="muted"><a href="./index_debug">← Torna a index_debug</a></div>
      </div>
      <div class="card" style="margin-top:12px">
        {items}
      </div>
    </div>
  </body>
</html>"""
    return html.encode("utf-8")


def _security_active_scenario(snapshot) -> tuple[str, str]:
    # Returns (scenario_description, scenario_code)
    entities = snapshot.get("entities") or []
    sys_ent = next((e for e in entities if str(e.get("type") or "") == "systems"), None)
    rt = (sys_ent or {}).get("realtime") or {}
    st = (sys_ent or {}).get("static") or {}
    arm = rt.get("ARM") or st.get("ARM") or {}
    if isinstance(arm, dict):
        desc = str(arm.get("D") or "").strip()
        code = str(arm.get("S") or "").strip()
        return (desc or "Sconosciuto", code or "?")
    return ("Sconosciuto", "?")


def render_security_ui(snapshot):
    scen_desc, scen_code = _security_active_scenario(snapshot)
    ui_tags = _load_ui_tags()

    scenarios = []
    for e in (snapshot.get("entities") or []):
        if str(e.get("type") or "").lower() != "scenarios":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        try:
            sid = int(e.get("id"))
        except Exception:
            continue
        scenarios.append(
            {
                "ID": sid,
                "DES": st.get("DES") or e.get("name") or f"Scenario {sid}",
                "CAT": str(st.get("CAT") or "").strip().upper(),
            }
        )
    scenarios.sort(key=lambda x: (str(x.get("CAT") or ""), str(x.get("DES") or "")))

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Security</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: rgba(255,255,255,0.65);
        --topbar-h: clamp(48px, 10vmin, 72px);
        --wrap-pb: clamp(14px, 4vmin, 32px);
        --center-gap: clamp(14px, 4vmin, 44px);
        --side-w: clamp(96px, 20vw, 140px);
        --side-icon: clamp(44px, 10vw, 62px);
        --side-label: clamp(14px, 4vw, 22px);
        --status-font: clamp(18px, 5vw, 28px);
        --status-mb: clamp(10px, 3.5vmin, 28px);
        /* Make the ring always fit the visible viewport (even on small/embedded tablets). */
        --ring-size: min(320px, max(140px, min(60vmin, calc(100vh - var(--topbar-h) - 220px))));
        --ring-w: clamp(10px, 3vmin, 21px);
        --addonver-fs: clamp(10px, 2.6vw, 12px);
        --lockimg: clamp(120px, 38vmin, 248px);
        --scen-mt: clamp(10px, 3.8vmin, 32px);
        --scen-fs: clamp(16px, 4.6vw, 24px);
        --alarmmem-mt: clamp(8px, 2.5vmin, 14px);
        --ring-track: rgba(255,255,255,0.14);
        --ring-ok: #1ed760;
        --ring-armed: #2a7fff;
        --ring-partial: #ffb020;
        --c-disarm: #00FF00;
        --c-total: #f92028;
        --c-partial: #0000FF;
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .splash {{
        position: fixed;
        top: 72px;
        left: 0;
        right: 0;
        bottom: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
        z-index: 10;
        opacity: 1;
        transition: opacity 350ms ease;
      }}
      .splash.hide {{
        opacity: 0;
        pointer-events: none;
      }}
      .splash img {{
        width: min(60vw, 520px);
        max-width: calc(100% - 64px);
        height: auto;
        object-fit: contain;
        opacity: 0.95;
        filter: drop-shadow(0 18px 60px rgba(0,0,0,0.65));
      }}
      .topbar {{
        position:fixed; top:0; left:0; right:0;
        display:flex; gap:18px; justify-content:center; align-items:center;
        height: var(--topbar-h);
        background: linear-gradient(to bottom, rgba(0,0,0,0.55), rgba(0,0,0,0));
        backdrop-filter: blur(8px);
        z-index: 2;
      }}
      /* Fallback for older/embedded browsers: don't rely on JS to reveal the UI. */
      @keyframes ks_splash_out {{
        to {{ opacity: 0; pointer-events: none; }}
      }}
      @keyframes ks_wrap_in {{
        to {{ opacity: 1; }}
      }}
      .splash {{
        animation: ks_splash_out 350ms ease 3.2s forwards;
      }}
      body.loading .wrap {{
        visibility: visible;
        opacity: 0;
        animation: ks_wrap_in 220ms ease 3.4s forwards;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{
        min-height: 100%;
        height:auto;
        display:flex;
        align-items:center;
        justify-content:center;
        padding: calc(var(--topbar-h) + 16px) 16px var(--wrap-pb);
        box-sizing: border-box;
      }}
      .center {{
        width: min(980px, 100%);
        display:flex;
        align-items:center;
        justify-content:center;
        gap: var(--center-gap);
      }}
      .sidebtn {{
        width: var(--side-w);
        text-align:center;
        color: rgba(255,255,255,0.85);
        user-select:none;
        cursor: pointer;
      }}
      .sidebtn .icon {{
        width: var(--side-icon); height: var(--side-icon);
        border-radius: 999px;
        display:inline-flex; align-items:center; justify-content:center;
        background: rgba(255,255,255,0.08);
        border: 1px solid rgba(255,255,255,0.12);
        backdrop-filter: blur(6px);
      }}
      .sidebtn .label {{ margin-top: 10px; font-size: var(--side-label); }}
      .main {{ text-align:center; flex: 1; min-width: 0; }}
      .statusline {{ font-size: var(--status-font); margin-bottom: var(--status-mb); }}
      .ring {{
        width: var(--ring-size);
        height: var(--ring-size);
        border-radius: 999px;
        display:flex;
        align-items:center;
        justify-content:center;
        margin: 0 auto;
        border: var(--ring-w) solid var(--ring-ok);
        box-shadow: none;
        cursor: pointer;
        position: relative;
        touch-action: manipulation;
        -webkit-tap-highlight-color: transparent;
      }}
      .addonVer {{
        position: absolute;
        bottom: 14px;
        left: 50%;
        transform: translateX(-50%);
        font-size: var(--addonver-fs);
        letter-spacing: 0.6px;
        color: rgba(255,255,255,0.55);
        user-select: none;
        pointer-events: none;
        white-space: nowrap;
      }}
      .ring.delay {{
        border-color: transparent;
      }}
      .ring.delay::before {{
        content: "";
        position: absolute;
        inset: 0;
        border-radius: 999px;
        background: conic-gradient(
          var(--ring-color, var(--c-total)) 0turn calc(var(--p, 0) * 1turn),
          var(--ring-track) calc(var(--p, 0) * 1turn) 1turn
        );
        -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - var(--ring-w)), #000 calc(100% - var(--ring-w)));
        mask: radial-gradient(farthest-side, transparent calc(100% - var(--ring-w)), #000 calc(100% - var(--ring-w)));
      }}
      .ring.state-disarm {{ border-color: var(--c-disarm); }}
      .ring.state-total {{ border-color: var(--c-total); }}
      .ring.state-partial {{ border-color: var(--c-partial); }}
      .lock {{
        opacity: 0.92;
        display: flex;
        align-items: center;
        justify-content: center;
        min-width: 1px;
        min-height: 1px;
        pointer-events: none;
      }}
      .lockText {{
        font-size: clamp(56px, 18vmin, 104px);
        font-weight: 300;
        letter-spacing: -2px;
        line-height: 1;
        color: rgba(255,255,255,0.92);
        user-select: none;
        pointer-events: none;
      }}
      .lockImg {{
        width: var(--lockimg);
        height: var(--lockimg);
        object-fit: contain;
        display: block;
        animation: none !important;
        pointer-events: none;
      }}
      .lockImg.alarm {{
        animation: alarmPulse 1.05s ease-in-out infinite !important;
      }}
      @keyframes alarmPulse {{
        0%   {{ opacity: 1; transform: scale(1); }}
        45%  {{ opacity: 0.25; transform: scale(1.06); }}
        100% {{ opacity: 1; transform: scale(1); }}
      }}
      .scen {{
        display: block;
        margin-top: var(--scen-mt);
        font-size: var(--scen-fs);
        color: rgba(255,255,255,0.92);
        min-height: 1em;
      }}
      .hint {{ margin-top: 10px; color: rgba(255,255,255,0.55); font-size: 14px; }}
      .alarmMem {{
        margin-top: var(--alarmmem-mt);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
        border-radius: 999px;
        border: 1px solid rgba(255, 80, 80, 0.35);
        background: rgba(255, 80, 80, 0.10);
        cursor: pointer;
        opacity: 0;
        pointer-events: none;
        transition: opacity 160ms ease;
      }}
      .alarmMem.show {{
        opacity: 1;
        pointer-events: auto;
      }}
      .alarmMem img {{
        width: 20px;
        height: 20px;
        display: block;
      }}
      .toast {{
        display: none;
        position: fixed;
        left: 50%;
        bottom: 22px;
        transform: translateX(-50%);
        background: rgba(0,0,0,0.78);
        border: 1px solid rgba(255,255,255,0.12);
        color: rgba(255,255,255,0.95);
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 13px;
        letter-spacing: 0.2px;
        z-index: 20;
        max-width: min(520px, calc(100% - 32px));
        text-align: center;
      }}
      .modal {{
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.72);
        backdrop-filter: blur(8px);
        display: none;
        align-items: center;
        justify-content: center;
        z-index: 5;
      }}
      .modal.show {{ display: flex; }}
      .sheet {{
        width: min(820px, calc(100% - 24px));
        max-height: min(680px, calc(100% - 24px));
        overflow: auto;
        overflow-x: hidden;
        background: rgba(14,16,22,0.95);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 16px;
        box-shadow: 0 22px 80px rgba(0,0,0,0.55);
      }}
      .sheet.mode-menu {{
        width: min(560px, calc(100% - 24px));
        max-height: 260px;
        overflow: hidden;
      }}
      .sheet.mode-list {{
        width: min(520px, calc(100% - 24px));
      }}
      .sheet.mode-pin {{
        width: min(560px, calc(100% - 24px));
        max-height: calc(100% - 24px);
        overflow: auto;
        overflow-x: hidden;
      }}
      .sheet.mode-menu .sheetHead,
      .sheet.mode-list .sheetHead,
      .sheet.mode-pin .sheetHead {{
        display: none;
      }}
      .sheet.mode-menu .sheetBody {{
        padding: 16px 14px 18px;
      }}
      .sheet.mode-menu .actionRow {{
        margin: 0;
        gap: 26px;
      }}
      .sheet.mode-menu .actionBtn .dot {{
        width: 76px;
        height: 76px;
        border-width: 3px;
      }}
      .sheet.mode-menu .actionBtn .dot svg {{
        width: 34px;
        height: 34px;
      }}
      .sheet.mode-menu .actionBtn .t {{
        font-size: 20px;
        margin-top: 10px;
      }}
      .sheet.mode-list .sheetBody {{
        padding: 14px 14px 14px;
      }}
      .sheet.mode-list .scFilter {{
        display: none;
      }}
      .sheet.mode-list .scRow {{
        margin-top: 8px;
      }}
      .sheet.mode-list .scMeta {{
        display: none;
      }}
      .sheetHead {{
        position: sticky;
        top: 0;
        background: rgba(14,16,22,0.92);
        backdrop-filter: blur(10px);
        padding: 14px 14px 10px;
        border-bottom: 1px solid rgba(255,255,255,0.08);
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 10px;
      }}
      .sheetTitle {{ font-size: 18px; color: rgba(255,255,255,0.92); }}
      .btn {{
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(255,255,255,0.06);
        color: rgba(255,255,255,0.9);
        padding: 8px 12px;
        border-radius: 12px;
        cursor: pointer;
        white-space: nowrap;
      }}
      .sheetBody {{ padding: 12px 14px 14px; }}
      .actionRow {{ display:flex; gap: 18px; justify-content:center; flex-wrap:wrap; margin: 10px 0 16px; }}
      .actionBtn {{ background: transparent; border: 0; color: rgba(255,255,255,0.92); cursor:pointer; }}
      .actionBtn .dot {{
        width: 96px; height: 96px; border-radius: 999px;
        display:flex; align-items:center; justify-content:center;
        border: 4px solid rgba(255,255,255,0.55);
        background: rgba(255,255,255,0.04);
      }}
      .actionBtn .dot {{ color: rgba(255,255,255,0.90); }}
      .actionBtn .dot svg {{ width: 40px; height: 40px; opacity: 0.98; }}
      .actionBtn .t {{ margin-top: 10px; font-size: 22px; }}
      .actionBtn[data-sec="ARM"] .dot {{ border-color: var(--c-total); color: var(--c-total); }}
      .actionBtn[data-sec="DISARM"] .dot {{ border-color: var(--c-disarm); color: var(--c-disarm); }}
      .actionBtn[data-sec="PARTIAL"] .dot {{ border-color: var(--c-partial); color: var(--c-partial); }}
      .scFilter {{
        width: 100%;
        box-sizing: border-box;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(0,0,0,0.28);
        color: rgba(255,255,255,0.92);
        outline: none;
      }}
      .scRow {{
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 10px;
        padding: 12px 12px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.08);
        background: rgba(255,255,255,0.04);
        margin-top: 10px;
      }}
      .scLeft {{ min-width: 0; }}
      .scName {{ font-size: 16px; color: rgba(255,255,255,0.92); white-space: nowrap; overflow: hidden; text-overflow: ellipsisipsis; }}
      .scMeta {{ font-size: 12px; color: rgba(255,255,255,0.58); margin-top: 4px; }}
      .pinCard {{
        width: 100%;
        max-width: 520px;
        box-sizing: border-box;
        margin: 0 auto;
        border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(255,255,255,0.04);
        padding: 14px 14px 12px;
        overflow-x: hidden;
      }}
      .pinDisplay {{
        margin-top: 10px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(0,0,0,0.28);
        padding: 12px 14px;
        font-size: 22px;
        letter-spacing: 6px;
        text-align: center;
        color: rgba(255,255,255,0.92);
        user-select: none;
        cursor: text;
      }}
      .keypad {{
        margin-top: 12px;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 10px;
      }}
      .key {{
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(255,255,255,0.06);
        color: rgba(255,255,255,0.92);
        padding: 12px 0;
        border-radius: 14px;
        font-size: 20px;
        cursor: pointer;
        width: 100%;
      }}
      .key:active {{
        transform: scale(0.98);
        background: rgba(255,255,255,0.10);
      }}
      .pinRow {{
        margin-top: 10px;
        display:flex;
        justify-content: flex-end;
        gap: 10px;
      }}
      .pinErr {{
        margin-top: 10px;
        border-radius: 12px;
        border: 1px solid rgba(255, 80, 80, 0.35);
        background: rgba(255, 80, 80, 0.10);
        color: rgba(255,255,255,0.92);
        padding: 10px 12px;
        font-size: 13px;
      }}
      @media (max-width: 820px) {{
        .center {{ gap: var(--center-gap); }}
      }}
      /* Extra-compact layout for small embedded tablets: reduce ring a bit more without affecting large screens. */
      @media (max-width: 900px) and (max-height: 600px) {{
        :root {{
          --ring-size: min(300px, max(140px, min(58vmin, calc(100vh - var(--topbar-h) - 240px))));
          --scen-mt: clamp(6px, 3.2vmin, 26px);
        }}
      }}
      @media (max-width: 560px) {{
        .center {{ flex-direction: column; gap: 14px; }}
        .sidebtn {{ width: auto; }}
        .sidebtn .label {{ font-size: 16px; }}
      }}
      @media (max-height: 420px) {{
        :root {{
          --topbar-h: 44px;
          --wrap-pb: 10px;
          --ring-size: clamp(150px, 52vmin, 220px);
          --lockimg: clamp(110px, 34vmin, 200px);
          --status-font: 18px;
          --status-mb: 6px;
          --scen-mt: 6px;
          --scen-fs: 16px;
          --alarmmem-mt: 6px;
        }}
        body {{ overflow-y: auto; }}
        .wrap {{
          padding-top: calc(var(--topbar-h) + 6px);
          align-items: flex-start;
          justify-content: center;
        }}
        .statusline {{ margin-bottom: 10px; }}
        .hint {{ display: none; }}
      }}
      @media (max-height: 360px) {{
        :root {{
          --topbar-h: 40px;
          --ring-size: clamp(130px, 48vmin, 190px);
          --lockimg: clamp(92px, 30vmin, 160px);
          --status-font: 16px;
          --scen-fs: 14px;
        }}
      }}
      @media (max-width: 520px) and (max-height: 520px) {{
        .center {{ gap: 14px; }}
      }}
      @media (max-width: 360px) {{
        .tab {{ font-size: 16px; padding: 8px 10px; }}
      }}
      @media (max-width: 520px) {{
        .modal {{ align-items: flex-end; }}
        .sheet {{
          width: 100%;
          max-height: calc(100% - 8px);
          border-radius: 16px 16px 0 0;
        }}
        .sheetBody {{ padding-bottom: calc(14px + env(safe-area-inset-bottom, 0px)); }}
        .sheet.mode-menu {{ width: 100%; }}
        .sheet.mode-list {{ width: 100%; }}
        .sheet.mode-pin {{ width: 100%; }}
        .pinCard {{ padding: 12px 12px 10px; border-radius: 14px; }}
        .pinDisplay {{ font-size: 20px; padding: 10px 12px; }}
        .key {{ padding: 10px 0; font-size: 18px; }}
        .pinRow {{ justify-content: center; flex-wrap: wrap; }}
        .pinRow .btn {{ flex: 1 1 120px; text-align: center; }}
      }}
    </style>
  </head>
  <body class="loading">
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div id="splash" class="splash" aria-hidden="true">
      <img src="/assets/logo_ekonex.png" alt="Ekonex"/>
    </div>
    <div class="topbar">
      <a class="tab active" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/scenarios">Scenari</a>
      <a class="tab" href="/security/sensors">Sensori</a>
    </div>

    <div class="wrap">
      <div class="center">
        <div class="sidebtn" id="btnEmergency">
          <div class="icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <path d="M12 2.4l2.7 5.4 6 0.9-4.3 4.2 1 6-5.4-2.8-5.4 2.8 1-6L3.3 8.7l6-0.9L12 2.4z" stroke="rgba(255,255,255,0.85)" stroke-width="1.5" stroke-linejoin="round"/>
            </svg>
          </div>
          <div class="label">Preferiti</div>
        </div>

        <div class="main">
          <div class="statusline" id="statusLine"></div>
          <div class="ring" id="ring" title="Inserisci/Disinserisci/Parziale">
            <div class="lock" id="lockIcon"></div>
            <div class="addonVer" id="addonVer">v{_html_escape(ADDON_VERSION)}</div>
          </div>
          <button class="alarmMem" id="alarmMem" type="button" title="Memoria allarme (dettagli)" aria-label="Memoria allarme">
            <img src="/assets/alarm" alt=""/>
          </button>
          <div class="scen" id="scenName"></div>
          <div class="hint" id="hint"></div>
        </div>

        <div class="sidebtn" id="btnFunctions">
          <div class="icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <path d="M5 7h14" stroke="rgba(255,255,255,0.85)" stroke-width="1.8" stroke-linecap="round"/>
              <path d="M5 12h14" stroke="rgba(255,255,255,0.85)" stroke-width="1.8" stroke-linecap="round"/>
              <path d="M5 17h14" stroke="rgba(255,255,255,0.85)" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="label">Funzioni</div>
        </div>
      </div>
    </div>

    <div class="toast" id="toast"></div>

    <div class="modal" id="modal">
      <div class="sheet">
        <div class="sheetHead">
          <div class="sheetTitle" id="sheetTitle">Sicurezza</div>
          <button class="btn" id="backBtn" style="display:none" type="button">Indietro</button>
          <button class="btn" id="closeBtn" type="button">Chiudi</button>
        </div>
        <div class="sheetBody">
          <div id="pinPane" style="display:none;">
            <div class="pinCard">
              <input class="scFilter" id="pinInput" type="password" autocomplete="off" placeholder="PIN" style="position:absolute; left:-9999px; width:1px; height:1px; opacity:0;" />
              <div class="pinDisplay" id="pinDisplay">••••</div>
              <div class="keypad" id="pinKeypad">
                <button class="key" type="button" data-k="1">1</button>
                <button class="key" type="button" data-k="2">2</button>
                <button class="key" type="button" data-k="3">3</button>
                <button class="key" type="button" data-k="4">4</button>
                <button class="key" type="button" data-k="5">5</button>
                <button class="key" type="button" data-k="6">6</button>
                <button class="key" type="button" data-k="7">7</button>
                <button class="key" type="button" data-k="8">8</button>
                <button class="key" type="button" data-k="9">9</button>
                <button class="key" type="button" data-k="back">⌫</button>
                <button class="key" type="button" data-k="0">0</button>
                <button class="key" type="button" data-k="enter">OK</button>
              </div>
              <div class="pinErr" id="pinErr" style="display:none;"></div>
            </div>
          </div>
          <div id="actionMenu" class="actionRow">
            <button class="actionBtn" data-sec="ARM" type="button">
              <div class="dot"><svg viewBox="0 0 24 24" fill="none"><path d="M12 2.8c2.8 2.1 5.9 2.5 8.2 2.7v6.9c0 4.7-3.1 8.1-8.2 8.8-5.1-.7-8.2-4.1-8.2-8.8V5.5c2.3-.2 5.4-.6 8.2-2.7z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M9.2 12.2l2 2.1 3.7-4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
              <div class="t">inserisci</div>
            </button>
            <button class="actionBtn" data-sec="DISARM" type="button">
              <div class="dot"><svg viewBox="0 0 24 24" fill="none"><path d="M8 10V8.2a4.8 4.8 0 0 1 9.2-1.9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M7.2 10.6h9.6A2.4 2.4 0 0 1 19.2 13v6.2a2.4 2.4 0 0 1-2.4 2.4H7.2A2.4 2.4 0 0 1 4.8 19.2V13a2.4 2.4 0 0 1 2.4-2.4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M10.4 16l1.6 1.7 3.2-3.6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
              <div class="t">disinserisci</div>
            </button>
            <button class="actionBtn" data-sec="PARTIAL" type="button">
              <div class="dot"><svg viewBox="0 0 24 24" fill="none"><path d="M12 2.8c2.8 2.1 5.9 2.5 8.2 2.7v6.9c0 4.7-3.1 8.1-8.2 8.8-5.1-.7-8.2-4.1-8.2-8.8V5.5c2.3-.2 5.4-.6 8.2-2.7z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 7.2v11.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M12 12.2l4-2.3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></div>
              <div class="t">parziale</div>
            </button>
          </div>
          <div id="scenarioPane" style="display:none;">
            <input class="scFilter" id="scFilter" placeholder="Cerca scenario..." />
            <div class="scList" id="scList"></div>
          </div>
          <div id="alarmListPane" style="display:none;">
            <div class="scList" id="alarmList"></div>
          </div>
        </div>
      </div>
    </div>

    <script>
      const toast = document.getElementById('toast');
      let toastTimer = null;
      function setToast(msg, ms=3500) {{
        if (!toast) return;
        toast.textContent = String(msg || '');
        toast.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{
          toast.style.display = 'none';
        }}, Number(ms || 3500));
      }}


      const SCENARIOS = {json.dumps(scenarios, ensure_ascii=False)};
      let UI_TAGS = {json.dumps(ui_tags, ensure_ascii=False)};

      function scenarioFromSystem(sysRt) {{
        const arm = (sysRt && sysRt.ARM) ? sysRt.ARM : null;
        if (!arm || typeof arm !== 'object') return {{desc:'Sconosciuto', code:'?'}};
        return {{desc: String(arm.D || 'Sconosciuto'), code: String(arm.S || '?')}};
      }}
      function systemStateLabel(code) {{
        const c = String(code || '').toUpperCase();
        if (c === 'D' || c === 'DISARM' || c === 'DISINSERITO') return 'Sistema disinserito';
        if (c === 'P' || c.startsWith('P_')) return 'Sistema parziale';
        if (c === 'T' || c.startsWith('T_')) return 'Sistema inserito';
        return 'Sistema';
      }}
      function isArmed(code) {{
        const c = String(code || '').toUpperCase();
        return !(c === 'D' || c === 'DISARM' || c === 'DISINSERITO');
      }}
      let lastEntities = [];
      let lastSystem = null;
      let alarmLatch = false;
      const delayState = {{
        active: false,
        kind: '',
        total: 0,
        baseRem: 0,
        baseAt: 0,
        label: '',
        color: '#f92028',
        fetchedAtZero: 0,
      }};

      function parsePartitionSeconds(raw) {{
        if (raw === null || raw === undefined) return 0;
        if (typeof raw === 'number' && Number.isFinite(raw)) {{
          if (Math.floor(raw) === raw) return Math.max(0, raw);
          const s = String(raw);
          if (s.includes('.')) {{
            const parts = s.split('.');
            const mm = Number(parts[0] || '0');
            const ss = Number(parts[1] || '0');
            if (Number.isFinite(mm) && Number.isFinite(ss) && ss >= 0 && ss < 60) return Math.max(0, mm * 60 + ss);
          }}
          return Math.max(0, Math.round(raw));
        }}
        const s = String(raw).trim();
        if (!s) return 0;
        if (s.includes(':')) {{
          const nums = s.split(':').map(p => Number(p));
          if (nums.some(n => !Number.isFinite(n))) return 0;
          if (nums.length === 2) return Math.max(0, nums[0] * 60 + nums[1]);
          if (nums.length === 3) return Math.max(0, nums[0] * 3600 + nums[1] * 60 + nums[2]);
        }}
        if (s.includes('.')) {{
          const parts = s.split('.');
          const mm = Number(parts[0] || '0');
          const ss = Number(parts[1] || '0');
          if (Number.isFinite(mm) && Number.isFinite(ss) && ss >= 0 && ss < 60) return Math.max(0, mm * 60 + ss);
        }}
        const n = Number(s);
        if (Number.isFinite(n)) return Math.max(0, Math.round(n));
        return 0;
      }}

      function maxPartitionSeconds(entities) {{
        let mx = 0;
        for (const e of (entities || [])) {{
          if (!e || String(e.type || '').toLowerCase() !== 'partitions') continue;
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
          const raw = rt.T ?? rt.TIME ?? 0;
          const n = parsePartitionSeconds(raw);
          if (Number.isFinite(n) && n > mx) mx = n;
        }}
        return mx;
      }}

      function isDelayCode(code) {{
        const c = String(code || '').toUpperCase();
        return c.endsWith('_OUT') || c.endsWith('_IN');
      }}

      function updateDelayState(code, entities) {{
        const c = String(code || '').toUpperCase();
        const delay = isDelayCode(c);
        if (!delay) {{
          delayState.active = false;
          delayState.kind = '';
          delayState.total = 0;
          delayState.baseRem = 0;
          delayState.baseAt = 0;
          delayState.fetchedAtZero = 0;
          return;
        }}
        const kind = c.endsWith('_OUT') ? 'OUT' : 'IN';
        const reportedRem = Math.max(0, Math.ceil(maxPartitionSeconds(entities)));
        const now = Date.now();

        if (!delayState.active || delayState.kind !== kind) {{
          delayState.active = true;
          delayState.kind = kind;
          delayState.total = reportedRem;
          delayState.baseRem = reportedRem;
          delayState.baseAt = now;
        }} else {{
          // NOTE: Ksenia might not stream the countdown every second; we "count down"
          // locally between updates. To keep it linear, only resync when the panel
          // provides a *lower* remaining time (or on a clear jump).
          if (reportedRem > 0) {{
            if (delayState.total <= 0) delayState.total = reportedRem;
            if (reportedRem > delayState.total) delayState.total = reportedRem;

            const cur = Number(delayState.baseRem || 0);
            // Resync when remaining decreases (normal case)
            if (cur <= 0 || reportedRem < (cur - 0.5)) {{
              delayState.baseRem = reportedRem;
              delayState.baseAt = now;
            }}
            // If remaining suddenly increases a lot, treat as a new delay start
            else if (reportedRem > (cur + 5)) {{
              delayState.baseRem = reportedRem;
              delayState.baseAt = now;
              if (delayState.total < reportedRem) delayState.total = reportedRem;
            }}
          }}
        }}

        const isPartial = (c.startsWith('P_') || c === 'P_OUT' || c === 'P_IN');
        delayState.color = isPartial ? '#0000FF' : '#f92028';
        delayState.label = (kind === 'OUT') ? "Tempo d'uscita" : "Tempo d'ingresso";
      }}

      function renderDelayUI() {{
        const ring = document.getElementById('ring');
        const lock = document.getElementById('lockIcon');
        if (!delayState.active) {{
          if (ring) {{
            ring.classList.remove('delay');
            ring.style.removeProperty('--p');
            ring.style.removeProperty('--ring-color');
          }}
          delayState.fetchedAtZero = 0;
          try {{ setDelayPolling(false); }} catch (_e) {{}}
          return;
        }}
        const now = Date.now();
        const elapsed = Math.max(0, (now - (delayState.baseAt || now)) / 1000);
        const rem = Math.max(0, Math.ceil((delayState.baseRem || 0) - elapsed));
        const total = Math.max(1, Math.ceil(delayState.total || rem || 1));
        let p = 0;
        if (delayState.kind === 'OUT') p = 1 - (rem / total);
        else p = (rem / total);
        if (!Number.isFinite(p)) p = 0;
        p = Math.max(0, Math.min(1, p));
        if (ring) {{
          ring.classList.add('delay');
          ring.style.setProperty('--p', String(p));
          ring.style.setProperty('--ring-color', delayState.color);
        }}
        if (lock) {{
          lock.textContent = '';
          const t = document.createElement('div');
          t.className = 'lockText';
          t.textContent = String(rem);
          lock.appendChild(t);
        }}

        // Watchdog: if we somehow stay in delay longer than expected, force a resync.
        if (delayState.baseAt && delayState.total && elapsed > (delayState.total + 8)) {{
          delayState.active = false;
          delayState.fetchedAtZero = 0;
          try {{ setDelayPolling(false); }} catch (_e) {{}}
          try {{ setTimeout(fetchSnap, 80); }} catch (_e) {{}}
          return;
        }}

        // When the local countdown reaches 0, force a snapshot to swap back to the icon
        // even if the panel does not emit a final realtime update.
        if (rem === 0) {{
          if (!delayState.fetchedAtZero || (now - delayState.fetchedAtZero) > 1500) {{
            delayState.fetchedAtZero = now;
            try {{ setTimeout(fetchSnap, 120); }} catch (_e) {{}}
          }}
        }} else {{
          delayState.fetchedAtZero = 0;
        }}
      }}

      setInterval(() => {{
        if (delayState.active) renderDelayUI();
      }}, 250);

      let pollTimer = null;
      let delayPollTimer = null;
      function setDelayPolling(on) {{
        if (on) {{
          if (!delayPollTimer) delayPollTimer = setInterval(fetchSnap, 900);
        }} else {{
          if (delayPollTimer) {{
            try {{ clearInterval(delayPollTimer); }} catch (_e) {{}}
            delayPollTimer = null;
          }}
        }}
      }}

      // When the user sends a command during delays (e.g. DISARM while countdown is active),
      // the panel may take a moment to publish the new state; proactively resync a few times.
      let resyncTimers = [];
      function scheduleResync() {{
        try {{
          for (const t of resyncTimers) {{ try {{ clearTimeout(t); }} catch (_e) {{}} }}
        }} catch (_e) {{}}
        resyncTimers = [];
        for (const d of [120, 480, 1100, 2200]) {{
          try {{
            resyncTimers.push(setTimeout(() => {{ try {{ fetchSnap(); }} catch (_e) {{}} }}, d));
          }} catch (_e) {{}}
        }}
      }}
      function afterCommandResync() {{
        try {{ delayState.active = false; }} catch (_e) {{}}
        try {{ delayState.fetchedAtZero = 0; }} catch (_e) {{}}
        try {{ setDelayPolling(false); }} catch (_e) {{}}
        try {{ renderDelayUI(); }} catch (_e) {{}}
        scheduleResync();
      }}

      function applySystem(s, entities) {{
        const scen = scenarioFromSystem(s);
        const code = String(scen.code || '').toUpperCase();
        const entList = Array.isArray(entities) ? entities : [];
        const useEntities = entList.length ? entList : lastEntities;
        const ring = document.getElementById('ring');
        if (ring) {{
          ring.classList.remove('state-disarm','state-total','state-partial');
          if (code === 'D' || code === 'DISARM' || code === 'DISINSERITO') ring.classList.add('state-disarm');
          else if (code === 'P' || code.startsWith('P_')) ring.classList.add('state-partial');
          else if (code === 'T' || code.startsWith('T_')) ring.classList.add('state-total');
        }}
        function alarmActive(sys, entities) {{
          // "ALARM" from STATUS_SYSTEM is not always populated; active alarm can also be inferred from zones (STA == 'A').
          if (sys) {{
            const a = sys.ALARM;
            if (typeof a === 'string') {{
              if (a.trim() !== '') return true;
            }}
            if (Array.isArray(a)) {{
              return a.length > 0;
            }}
            if (a && typeof a === 'object') {{
              if (Object.keys(a).length > 0) return true;
              for (const v of Object.values(a)) {{
                if (Array.isArray(v) && v.length > 0) return true;
                if (v && typeof v === 'object') {{
                  for (const vv of Object.values(v)) {{
                    if (Array.isArray(vv) && vv.length > 0) return true;
                  }}
                }}
              }}
            }}
          }}
          if (!Array.isArray(entities)) return false;
          for (const e of entities) {{
            if (String(e.type || '').toLowerCase() !== 'partitions') continue;
            const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
            const ast = String(rt.AST || '').toUpperCase();
            const arm = String(rt.ARM || '').toUpperCase();
            if (ast === 'AL' || ast === 'ALARM') return true;
            if (arm === 'AL' || arm === 'ALARM') return true;
          }}
          for (const e of entities) {{
            if (String(e.type || '').toLowerCase() !== 'zones') continue;
            const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
            const sta = String(rt.STA || '').toUpperCase();
            if (sta === 'A') return true;
          }}
          return false;
        }}
        function alarmMemActive(sys) {{
          if (!sys) return false;
          const a = sys.ALARM_MEM;
          if (Array.isArray(a)) {{
            return a.length > 0;
          }}
          if (a && typeof a === 'object') {{
            for (const v of Object.values(a)) {{
              if (Array.isArray(v) && v.length > 0) return true;
              if (v && typeof v === 'object') {{
                for (const vv of Object.values(v)) {{
                  if (Array.isArray(vv) && vv.length > 0) return true;
                }}
              }}
            }}
          }}
          return false;
        }}

        if (entList.length) lastEntities = entList;
        lastSystem = s || null;
        updateDelayState(code, useEntities);
        setDelayPolling(delayState.active);
        const memList = alarmEventsSinceReset(lastEntities);
        const inAlarmMem = alarmMemActive(s) || memList.length > 0;
        const inAlarmRaw = alarmActive(s, useEntities);
        const isDisarmed = (code === 'D' || code === 'DISARM' || code === 'DISINSERITO');
        function allPartitionsDisarmed(ents) {{
          if (!Array.isArray(ents)) return false;
          let saw = false;
          for (const e of ents) {{
            if (String(e.type || '').toLowerCase() !== 'partitions') continue;
            saw = true;
            const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
            const arm = String(rt.ARM || '').toUpperCase();
            if (arm && arm !== 'D' && arm !== 'DISARM' && arm !== 'DISINSERITO') return false;
          }}
          return saw;
        }}
        if (inAlarmRaw) alarmLatch = true;
        if (alarmLatch && allPartitionsDisarmed(useEntities)) alarmLatch = false;
        const inAlarm = (!isDisarmed && (inAlarmRaw || alarmLatch));
        const icon = inAlarm
          ? '/assets/alarm'
          : (code === 'D' || code === 'DISARM' || code === 'DISINSERITO')
            ? '/assets/disarm'
            : (code === 'P' || code.startsWith('P_'))
              ? '/assets/partial'
              : (code === 'T' || code.startsWith('T_'))
                ? '/assets/arm'
                : '/assets/arm';
        const lock = document.getElementById('lockIcon');
        const verEl = document.getElementById('addonVer');
        const memEl = document.getElementById('alarmMem');
        if (verEl) verEl.style.display = delayState.active ? 'none' : 'block';
        if (memEl) memEl.classList.toggle('show', !!inAlarmMem);
        if (lock) {{
          const wantAlarm = inAlarm ? '1' : '0';
          const wantSrc = String(icon || '');
          const curAlarm = lock.dataset.alarm || '0';
          const curSrc = lock.dataset.src || '';

          // Avoid flicker: only touch DOM when icon/alarm state changes.
          if (delayState.active) {{
            // During delays we show remaining seconds instead of icon.
            lock.textContent = '';
            lock.dataset.alarm = '0';
            lock.dataset.src = '';
          }} else if (curAlarm !== wantAlarm || curSrc !== wantSrc || !lock.firstElementChild) {{
            lock.textContent = '';
            const img = document.createElement('img');
            img.className = 'lockImg' + (inAlarm ? ' alarm' : '');
            img.alt = '';
            img.src = wantSrc;
            lock.appendChild(img);
            lock.dataset.alarm = wantAlarm;
            lock.dataset.src = wantSrc;
          }} else {{
            // Ensure class is coherent even if DOM was modified elsewhere.
            const img = lock.firstElementChild;
            if (img) img.className = 'lockImg' + (inAlarm ? ' alarm' : '');
          }}
        }}
        document.getElementById('statusLine').textContent = delayState.active ? delayState.label : systemStateLabel(code);
        const scenEl = document.getElementById('scenName');
        if (scenEl) scenEl.textContent = (scen.desc || '');
        renderDelayUI();
      }}

      // Splash: show logo for ~2–3s while we sync state.
      const splash = document.getElementById('splash');
      const splashStart = Date.now();
      let splashHidden = false;
      function hideSplash() {{
        if (!splash || splashHidden) return;
        const elapsed = Date.now() - splashStart;
        const wait = Math.max(0, 2400 - elapsed);
        setTimeout(() => {{
          if (!splash || splashHidden) return;
          splashHidden = true;
          document.body.classList.remove('loading');
          splash.classList.add('hide');
          setTimeout(() => {{ try {{ splash.remove(); }} catch (_e) {{}} }}, 500);
        }}, wait);
      }}
      setTimeout(hideSplash, 3000);

      async function fetchSnap() {{
        if (fetchSnap._inFlight) return;
        fetchSnap._inFlight = true;
        try {{
          const res = await fetch('/api/entities', {{cache:'no-store'}});
          const snap = await res.json();
          const sys = (snap.entities || []).find(e => String(e.type||'') === 'systems');
          applySystem((sys && sys.realtime) ? sys.realtime : (sys && sys.static) ? sys.static : null, snap.entities || []);
          hideSplash();
          lastApplyAt = Date.now();
        }} catch (e) {{
          setToast('Errore lettura stato: ' + e);
        }} finally {{
          fetchSnap._inFlight = false;
        }}
      }}

      let sse = null;
      let sseRetryTimer = null;
      let lastApplyAt = 0;
      function startPolling(intervalMs) {{
        if (pollTimer) {{
          try {{ clearInterval(pollTimer); }} catch (_e) {{}}
          pollTimer = null;
        }}
        if (intervalMs && intervalMs > 0) pollTimer = setInterval(fetchSnap, intervalMs);
      }}
      function startSSE() {{
        if (!window.EventSource) return false;
        if (sse) return true;
        try {{ sse = new EventSource('/api/stream'); }} catch (_e) {{ sse = null; return false; }}
        sse.onopen = () => {{
          // Keep a polling fallback even when SSE is connected: SSE events are deltas
          // and may not include all entities needed by this view.
          startPolling(2500);
        }};
        sse.onmessage = (ev) => {{
          try {{
            const msg = JSON.parse(ev.data || '{{}}');
            const ents = msg.entities || [];
            const sys = (ents || []).find(e => String(e.type||'') === 'systems');
            if (sys) {{
              applySystem(sys.realtime || sys.static || null, ents || []);
              hideSplash();
              lastApplyAt = Date.now();
            }} else {{
              // Some updates may not include the system entity; fall back to a full snapshot.
              fetchSnap();
            }}
          }} catch (_e) {{}}
        }};
        sse.onerror = () => {{
          try {{ if (sse) sse.close(); }} catch (_e) {{}}
          sse = null;
          // Force a resync soon (helps when WS reconnects server-side).
          fetchSnap();
          startPolling(2500);
          if (sseRetryTimer) return;
          sseRetryTimer = setTimeout(() => {{
            sseRetryTimer = null;
            startSSE();
          }}, 2000);
        }};
        return true;
      }}
      startPolling(2500);
      startSSE();
      fetchSnap();
      // Keep tag dropdown options in sync with Tag Styles.
      try {{ refreshTagSelectOptions(); setInterval(refreshTagSelectOptions, 15000); }} catch (_e) {{}}

      // Watchdog: if UI hasn't received updates for a while, force a refresh.
      setInterval(() => {{
        if (document.hidden) return;
        const age = Date.now() - (lastApplyAt || 0);
        if (age > 8000) fetchSnap();
      }}, 1000);

      window.addEventListener('focus', () => fetchSnap());

      function alarmCauseFromLogs(entities) {{
        if (!Array.isArray(entities)) return null;
        let best = null;
        let bestTs = 0;
        for (const e of entities) {{
          if (String(e.type || '').toLowerCase() !== 'logs') continue;
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
          const typ = String(rt.TYPE || '').toUpperCase();
          const ev = String(rt.EV || '');
          const evLower = ev.toLowerCase();
          const isAlarmType = (typ === 'ZALARM' || typ === 'ALARM');
          const isAlarmEvent = evLower.includes('allarme zona');
          const isTiming = evLower.includes("tempo d'uscita") || evLower.includes("tempo d'ingresso");
          const isAlarm = (isAlarmType || isAlarmEvent) && !isTiming;
          if (!isAlarm) continue;
          const ts = Number(e.last_seen || 0);
          if (ts >= bestTs) {{
            bestTs = ts;
            best = rt;
          }}
        }}
        return best;
      }}

      function isAlarmResetLog(rt) {{
        const typ = String(rt.TYPE || '').toUpperCase();
        const ev = String(rt.EV || '').toLowerCase();
        if (typ.includes('RESET')) return true;
        if (ev.includes('reset allarmi')) return true;
        if (ev.includes('reset allarme')) return true;
        if (ev.includes('reset memoria')) return true;
        if (ev.includes('ripristino memoria')) return true;
        return false;
      }}

      function alarmEventsSinceReset(entities) {{
        if (!Array.isArray(entities)) return [];
        const logs = [];
        for (const e of entities) {{
          if (String(e.type || '').toLowerCase() !== 'logs') continue;
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
          const typ = String(rt.TYPE || '').toUpperCase();
          const ev = String(rt.EV || '');
          const evLower = ev.toLowerCase();
          const isAlarmType = (typ === 'ZALARM' || typ === 'ALARM');
          const isAlarmEvent = evLower.includes('allarme zona');
          const isTiming = evLower.includes("tempo d'uscita") || evLower.includes("tempo d'ingresso");
          if ((isAlarmType || isAlarmEvent) && !isTiming) {{
            logs.push({{ ts: Number(e.last_seen || 0), rt }});
          }}
        }}
        if (!logs.length) return [];
        let resetTs = 0;
        let resetId = 0;
        for (const e of entities) {{
          if (String(e.type || '').toLowerCase() !== 'logs') continue;
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
          if (!isAlarmResetLog(rt)) continue;
          const ts = Number(e.last_seen || 0);
          if (ts > resetTs) resetTs = ts;
          const rid = Number(rt.ID || 0);
          if (rid > resetId) resetId = rid;
        }}
        const out = logs
          .filter(x => (!resetId || Number(x.rt.ID || 0) >= resetId) && (!resetTs || x.ts >= resetTs))
          .sort((a, b) => (a.ts || 0) - (b.ts || 0))
          .map(x => x.rt);
        return out;
      }}

      function alarmCauseFromZones(entities) {{
        if (!Array.isArray(entities)) return null;
        const zones = [];
        for (const e of entities) {{
          if (String(e.type || '').toLowerCase() !== 'zones') continue;
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
          const sta = String(rt.STA || '').toUpperCase();
          if (sta !== 'A') continue;
          const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
          const name = String(e.name || st.DES || ('Zona ' + String(e.id || '')));
          zones.push(name);
        }}
        if (!zones.length) return null;
        return 'Zone in allarme: ' + zones.join(', ');
      }}

      function alarmCauseText() {{
        const list = alarmEventsSinceReset(lastEntities);
        if (list.length) {{
          if (list.length === 1) {{
            const log = list[0];
            const when = [log.DATA, log.TIME].filter(Boolean).join(' ');
            const ev = String(log.EV || '').trim();
            const i1 = String(log.I1 || '').trim();
            const i2 = String(log.I2 || '').trim();
            const parts = [];
            if (when) parts.push(when);
            if (ev) parts.push(ev);
            if (i1) parts.push(i1);
            if (i2) parts.push(i2);
            return parts.join(' - ') || 'Memoria allarme attiva';
          }}
          const lines = list.map((log) => {{
            const when = [log.DATA, log.TIME].filter(Boolean).join(' ');
            const ev = String(log.EV || '').trim();
            const i1 = String(log.I1 || '').trim();
            const parts = [];
            if (when) parts.push(when);
            if (ev) parts.push(ev);
            if (i1) parts.push(i1);
            return parts.join(' - ') || 'Allarme zona';
          }});
          return lines.join('\\n');
        }}
        const zoneText = alarmCauseFromZones(lastEntities);
        if (zoneText) return zoneText;
        return 'Memoria allarme attiva';
      }}

      const alarmMemBtn = document.getElementById('alarmMem');
      if (alarmMemBtn) {{
        alarmMemBtn.addEventListener('click', () => {{
          const msg = alarmCauseText();
          if (msg.includes('\\n')) {{
            const lines = msg.split('\\n');
            modal.classList.add('show');
            openAlarmList(lines);
          }} else {{
            setToast(msg);
          }}
        }});
      }}

      // PIN session (same model as index_debug)
      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{ return null; }}
      }}
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        // Ingress paths look like: /api/hassio_ingress/<token>/...
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}

      async function loadFavorites() {{
        try {{
          const res = await fetch('/api/ui_favorites', {{ cache: 'no-store' }});
          const data = await res.json();
          if (data && typeof data === 'object') favorites = data;
        }} catch (_e) {{}}
      }}
      function isFav(type, id) {{
        const bucket = (favorites && favorites[type] && typeof favorites[type] === 'object') ? favorites[type] : {{}};
        return !!bucket[String(id)];
      }}
      async function toggleFav(type, id, ev) {{
        try {{ if (ev) ev.stopPropagation(); }} catch (_e) {{}}
        const fav = !isFav(type, id);
        try {{
          await fetch('/api/ui_favorites', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ type, id, fav }}),
          }});
          if (!favorites[type] || typeof favorites[type] !== 'object') favorites[type] = {{}};
          if (fav) favorites[type][String(id)] = true;
          else delete favorites[type][String(id)];
          applyFavUi();
        }} catch (e) {{
          console.error('toggleFav error', e);
        }}
      }}
      function applyFavUi() {{
        document.querySelectorAll('button.fav[data-key]').forEach((btn) => {{
          const key = String(btn.getAttribute('data-key') || '');
          const parts = key.split(':');
          const t = parts[0] || '';
          const i = parts[1] || '';
          const on = isFav(t, i);
          btn.classList.toggle('on', on);
          btn.textContent = on ? '★' : '☆';
        }});
      }}

      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch(apiUrl('/api/cmd'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : txt);
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      let pinFlow = null; // {{ promise, resolve, reject, prevMode, prevSecCat }}
      function cancelPinFlow(reason) {{
        if (!pinFlow) return;
        try {{ pinFlow.reject(reason || new Error('PIN annullato')); }} catch (_e) {{}}
        pinFlow = null;
      }}
      function showPinError(msg) {{
        const pinErr = document.getElementById('pinErr');
        if (!pinErr) return;
        pinErr.textContent = String(msg || 'Errore');
        pinErr.style.display = '';
      }}
      function hidePinError() {{
        const pinErr = document.getElementById('pinErr');
        if (!pinErr) return;
        pinErr.textContent = '';
        pinErr.style.display = 'none';
      }}
      function openPinPane() {{
        if (pinFlow && pinFlow.promise) return pinFlow.promise;
        const pinPane = document.getElementById('pinPane');
        const pinInput = document.getElementById('pinInput');
        const pinDisplay = document.getElementById('pinDisplay');
        const pinKeypad = document.getElementById('pinKeypad');
        const sheetTitle = document.getElementById('sheetTitle');
        const backBtn = document.getElementById('backBtn');
        const actionMenu = document.getElementById('actionMenu');
        const scenarioPane = document.getElementById('scenarioPane');
        const modalEl = document.getElementById('modal');

        const prevMode = typeof modalMode === 'string' ? modalMode : 'SEC_MENU';
        const prevSecCat = typeof secCat === 'string' ? secCat : 'ARM';

        pinFlow = {{ promise: null, resolve: null, reject: null, prevMode, prevSecCat }};
        pinFlow.promise = new Promise((resolve, reject) => {{ pinFlow.resolve = resolve; pinFlow.reject = reject; }});

        hidePinError();
        try {{ setSheetMode('pin'); }} catch (_e) {{}}
        if (pinPane) pinPane.style.display = '';
        if (actionMenu) actionMenu.style.display = 'none';
        if (scenarioPane) scenarioPane.style.display = 'none';
        if (sheetTitle) sheetTitle.textContent = '';
        if (backBtn) backBtn.style.display = 'none';
        if (modalEl) modalEl.classList.add('show');
        let pinValue = '';
        let revealTimer = null;
        function maskForDisplay(value, revealLast) {{
          const s = String(value || '');
          if (!s) return '••••';
          if (revealLast && s.length >= 1) {{
            return '•'.repeat(Math.max(0, s.length - 1)) + s.slice(-1);
          }}
          return '•'.repeat(s.length);
        }}
        function setPinValue(next, revealLast) {{
          pinValue = String(next || '').replace(/\\D/g, '').slice(0, 12);
          if (pinInput) pinInput.value = pinValue;
          if (pinDisplay) pinDisplay.textContent = maskForDisplay(pinValue, !!revealLast);
          if (revealTimer) {{ try {{ clearTimeout(revealTimer); }} catch (_e) {{}} }}
          if (revealLast && pinValue) {{
            revealTimer = setTimeout(() => {{
              if (pinDisplay) pinDisplay.textContent = maskForDisplay(pinValue, false);
            }}, 1000);
          }}
        }}
        setPinValue('', false);
        if (pinInput) {{
          // Prevent OS keyboard: never focus the hidden input, only keep it for autofill/pass managers if any.
          pinInput.oninput = () => {{
            const v = String(pinInput.value || '');
            setPinValue(v, true);
          }};
          pinInput.onkeydown = (ev) => {{
            if (ev && ev.key === 'Enter') doOk();
          }};
        }}
        if (pinDisplay) {{
          pinDisplay.onclick = () => {{ /* no-op: avoid OS keyboard */ }};
        }}
        if (pinKeypad) {{
          for (const b of pinKeypad.querySelectorAll('button[data-k]')) {{
            b.onclick = () => {{
              const k = String(b.getAttribute('data-k') || '');
              if (k === 'back') {{
                setPinValue(pinValue.slice(0, -1), false);
                return;
              }}
              if (k === 'enter') {{ doOk(); return; }}
              if (/^\\d$/.test(k)) {{
                setPinValue(pinValue + k, true);
              }}
            }};
          }}
        }}

        async function doOk() {{
          const pin = String(pinValue || (pinInput ? pinInput.value : '') || '').trim();
          if (!pin) {{ showPinError('Inserisci il PIN'); return; }}
          hidePinError();
          try {{
            const token = await startPinSession(pin, 5);
            const restoreMode = pinFlow ? pinFlow.prevMode : prevMode;
            const restoreCat = pinFlow ? pinFlow.prevSecCat : prevSecCat;
            if (pinPane) pinPane.style.display = 'none';
            if (restoreMode === 'SEC_LIST') openSecurityList(restoreCat);
            else if (restoreMode === 'HOME_LIST') openSmarthomeList();
            else openSecurityMenu();
            if (pinFlow && pinFlow.resolve) pinFlow.resolve(token);
            pinFlow = null;
          }} catch (e) {{
            showPinError('PIN non valido o errore: ' + (e && e.message ? e.message : e));
          }}
        }}
        return pinFlow.promise;
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        return await openPinPane();
      }}
      async function sendCmd(type, id, action) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        const token = getPinSessionToken();
        if (token) payload.token = token;
        const res = await fetch(apiUrl('/api/cmd'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
        const text = await res.text();
        let data = null;
        try {{ data = JSON.parse(text); }} catch (_e) {{}}
        const errRaw = data && data.error !== undefined ? String(data.error) : '';
        const err = errRaw.trim().toLowerCase();
        const needsPin = (
          err === 'pin_session_required' ||
          err === 'invalid_token' ||
          err === 'login_failed' ||
          (data && data.ok === false && err === '')
        );
        if (needsPin) {{
          try {{ localStorage.removeItem(PIN_TOKEN_KEY); localStorage.removeItem(PIN_EXP_KEY); }} catch (_e) {{}}
          await ensurePinSession();
          return await sendCmd(type, id, action);
        }}
        return {{ ok: res.ok, text, data }};
      }}

      // Modal / scenario selection
      const modal = document.getElementById('modal');
      const sheet = document.querySelector('#modal .sheet');
      const sheetTitle = document.getElementById('sheetTitle');
      const backBtn = document.getElementById('backBtn');
      const closeBtn = document.getElementById('closeBtn');
      const actionMenu = document.getElementById('actionMenu');
      const scenarioPane = document.getElementById('scenarioPane');
      const scFilter = document.getElementById('scFilter');
      const scList = document.getElementById('scList');
      const alarmListPane = document.getElementById('alarmListPane');
      const alarmList = document.getElementById('alarmList');

      let modalMode = 'SEC_MENU'; // SEC_MENU | SEC_LIST | HOME_LIST | ALARM_LIST
      let secCat = 'ARM';

      function setSheetMode(mode) {{
        if (!sheet) return;
        sheet.classList.remove('mode-menu', 'mode-list', 'mode-pin');
        sheet.classList.add('mode-' + String(mode || 'menu'));
      }}

      function isSecurityCat(cat) {{
        const c = String(cat || '').toUpperCase();
        return (c === 'ARM' || c === 'DISARM' || c === 'PARTIAL');
      }}

      function openSecurityMenu() {{
        modalMode = 'SEC_MENU';
        setSheetMode('menu');
        sheetTitle.textContent = '';
        backBtn.style.display = 'none';
        actionMenu.style.display = 'flex';
        scenarioPane.style.display = 'none';
        if (alarmListPane) alarmListPane.style.display = 'none';
        scFilter.value = '';
        scList.innerHTML = '';
      }}
      async function ensureScenariosLoaded() {{
        try {{
          try {{
            const tagRes = await fetch('/api/ui_tags', {{ cache:'no-store' }});
            if (tagRes.ok) {{
              const tags = await tagRes.json();
              if (tags && typeof tags === 'object') UI_TAGS = tags;
            }}
          }} catch (_e) {{}}
          if (Array.isArray(SCENARIOS) && SCENARIOS.length > 0) return true;
          const res = await fetch('/api/entities', {{ cache:'no-store' }});
          if (!res.ok) return false;
          const snap = await res.json();
          const next = [];
          for (const e of (snap.entities || [])) {{
            if (String(e.type || '').toLowerCase() !== 'scenarios') continue;
            const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
            const sid = Number(e.id);
            if (!Number.isFinite(sid)) continue;
            const des = String(st.DES || e.name || ('Scenario ' + sid));
            const cat = String(st.CAT || '').trim().toUpperCase();
            next.push({{ ID: sid, DES: des, CAT: cat }});
          }}
          next.sort((a,b) => (String(a.CAT||'').localeCompare(String(b.CAT||'')) || String(a.DES||'').localeCompare(String(b.DES||''))));
          if (Array.isArray(SCENARIOS)) {{
            SCENARIOS.length = 0;
            for (const it of next) SCENARIOS.push(it);
          }}
          return SCENARIOS.length > 0;
        }} catch (_e) {{
          return false;
        }}
      }}

      function openSecurityList(cat) {{
        modalMode = 'SEC_LIST';
        secCat = String(cat || 'ARM').toUpperCase();
        setSheetMode('list');
        sheetTitle.textContent = (secCat === 'DISARM') ? 'Disinserisci' : (secCat === 'PARTIAL') ? 'Parziale' : 'Inserisci';
        backBtn.style.display = '';
        actionMenu.style.display = 'none';
        scenarioPane.style.display = '';
        if (alarmListPane) alarmListPane.style.display = 'none';
        scFilter.value = '';
        renderScenarioList('');
      }}
      function openSmarthomeList() {{
        modalMode = 'HOME_LIST';
        setSheetMode('list');
        sheetTitle.textContent = 'Smarthome';
        backBtn.style.display = 'none';
        actionMenu.style.display = 'none';
        scenarioPane.style.display = '';
        if (alarmListPane) alarmListPane.style.display = 'none';
        scFilter.value = '';
        renderScenarioList('');
      }}

      function openAlarmList(lines) {{
        modalMode = 'ALARM_LIST';
        setSheetMode('list');
        sheetTitle.textContent = 'Memoria allarme';
        backBtn.style.display = '';
        actionMenu.style.display = 'none';
        scenarioPane.style.display = 'none';
        if (alarmListPane) alarmListPane.style.display = '';
        if (scFilter) scFilter.value = '';
        if (alarmList) {{
          const items = (Array.isArray(lines) ? lines : []).filter(Boolean);
          alarmList.innerHTML = items.map(line => `
            <div class="scRow">
              <div class="scLeft">
                <div class="scName">${{escapeHtml(String(line))}}</div>
              </div>
            </div>
          `).join('') || '<div class="muted">Nessun allarme</div>';
        }}
      }}

      function renderScenarioList(q) {{
        const needle = String(q||'').toLowerCase();
        let items = SCENARIOS;
        if (modalMode === 'SEC_LIST') items = items.filter(s => String(s.CAT||'').toUpperCase() === secCat);
        if (modalMode === 'HOME_LIST') items = items.filter(s => !isSecurityCat(s.CAT));
        const tagMap = (UI_TAGS && UI_TAGS.scenarios && typeof UI_TAGS.scenarios === 'object') ? UI_TAGS.scenarios : {{}};
        items = items.filter(s => {{
          const entry = tagMap[String(s.ID)];
          return !(entry && entry.visible === false);
        }});
        items = items.filter(s => String(s.DES||'').toLowerCase().includes(needle));
        scList.innerHTML = items.map(s => `
          <div class="scRow">
            <div class="scLeft">
              <div class="scName">${{escapeHtml(s.DES)}}</div>
              <div class="scMeta">ID ${{escapeHtml(String(s.ID))}} &middot; ${{escapeHtml(String(s.CAT||''))}}</div>
            </div>
            <button class="btn" data-sid="${{escapeHtml(String(s.ID))}}" type="button">Esegui</button>
          </div>
        `).join('') || '<div class="muted">Nessuno scenario</div>';
        for (const btn of scList.querySelectorAll('button[data-sid]')) {{
          btn.addEventListener('click', async (ev) => {{
            const target = ev.currentTarget || ev.target;
            if (!target) return;
            const sid = Number(target.getAttribute('data-sid'));
            setToast('Eseguo scenario ' + sid + '...');
            const res = await sendCmd('scenarios', sid, 'execute');
            const ok = !!(res && (res.ok === true) && (!res.data || res.data.ok !== false));
            setToast(ok ? 'OK' : ('Errore: ' + (res && res.data && res.data.error ? res.data.error : res.text)));
            afterCommandResync();
            modal.classList.remove('show');
          }});
        }}
      }}

      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}

      function closeModal() {{
        if (pinFlow) {{
          try {{ cancelPinFlow(new Error('Modal chiusa')); }} catch (_e) {{}}
          try {{ document.getElementById('pinPane').style.display = 'none'; }} catch (_e) {{}}
        }}
        if (alarmListPane) {{
          try {{ alarmListPane.style.display = 'none'; }} catch (_e) {{}}
          if (alarmList) alarmList.innerHTML = '';
        }}
        try {{ setSheetMode('menu'); }} catch (_e) {{}}
        modal.classList.remove('show');
      }}

      document.getElementById('ring').addEventListener('click', () => {{ modal.classList.add('show'); openSecurityMenu(); }});
      // Reload/torna alla home security quando si preme "Stato".
      try {{
        const tabState = document.querySelector('.topbar .tab[href="/security"]');
        if (tabState) {{
          tabState.addEventListener('click', (ev) => {{
            ev.preventDefault();
            window.location.href = '/security';
          }});
        }}
      }} catch (_e) {{}}
      const btnEmergency = document.getElementById('btnEmergency');
      if (btnEmergency) {{
        btnEmergency.addEventListener('click', () => {{ window.location.href = '/security/favorites'; }});
      }}
      const btnFunctions = document.getElementById('btnFunctions');
      if (btnFunctions) {{
        btnFunctions.addEventListener('click', () => {{ window.location.href = '/security/functions'; }});
      }}
      closeBtn.addEventListener('click', () => closeModal());
      backBtn.addEventListener('click', () => openSecurityMenu());
      modal.addEventListener('click', (ev) => {{ if (ev.target === modal) closeModal(); }});
      for (const btn of document.querySelectorAll('button[data-sec]')) {{
        btn.addEventListener('click', async (ev) => {{
          const btnEl = ev.currentTarget || ev.target;
          const sec = btnEl ? btnEl.getAttribute('data-sec') : null;
          await ensureScenariosLoaded();
          openSecurityList(sec);
        }});
      }}
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security(snapshot):
    scen_desc, scen_code = _security_active_scenario(snapshot)

    # Precompute scenario list for the picker (client-side still renders the UI, but we seed data).
    scenarios = []
    for e in (snapshot.get("entities") or []):
        if str(e.get("type") or "").lower() != "scenarios":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        try:
            sid = int(e.get("id"))
        except Exception:
            continue
        scenarios.append(
            {
                "ID": sid,
                "DES": st.get("DES") or e.get("name") or f"Scenario {sid}",
                "CAT": str(st.get("CAT") or "").strip().upper(),
            }
        )
    scenarios.sort(key=lambda x: (str(x.get("CAT") or ""), str(x.get("DES") or "")))

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Security</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --ring-ok: #1ed760;
        --ring-warn: #ffb020;
        --ring-bad: #ff4d4d;
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:fixed; top:0; left:0; right:0;
        display:flex; gap:18px; justify-content:center; align-items:center;
        height:72px;
        background: linear-gradient(to bottom, rgba(0,0,0,0.55), rgba(0,0,0,0));
        backdrop-filter: blur(8px);
        z-index: 2;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{
        height:100%;
        display:flex;
        align-items:center;
        justify-content:center;
        padding: 88px 20px 32px;
        box-sizing: border-box;
      }}
      .center {{
        width: min(980px, 100%);
        display:flex;
        align-items:center;
        justify-content:center;
        gap: 44px;
      }}
      .center.stack {{
        flex-direction: column;
        gap: 16px;
      }}
      .sidebtn {{
        width: 140px;
        text-align:center;
        color: rgba(255,255,255,0.85);
        user-select:none;
      }}
      .sidebtn .icon {{
        width: 62px; height: 62px;
        border-radius: 999px;
        display:inline-flex; align-items:center; justify-content:center;
        background: rgba(255,255,255,0.08);
        border: 1px solid rgba(255,255,255,0.12);
        backdrop-filter: blur(6px);
      }}
      .sidebtn .label {{ margin-top: 10px; font-size: 22px; }}
      .main {{
        text-align:center;
        flex: 1;
      }}
      .statusline {{
        font-size: 28px;
        color: var(--ring-ok);
        margin-bottom: 18px;
      }}
      .ring {{
        width: 320px;
        height: 320px;
        border-radius: 999px;
        display:flex;
        align-items:center;
        justify-content:center;
        margin: 0 auto;
        border: 6px solid var(--ring-ok);
        box-shadow: 0 0 0 18px rgba(30,215,96,0.06);
        cursor: pointer;
      }}
      .ring .inner {{
        width: 260px;
        height: 260px;
        border-radius: 999px;
        background: rgba(0,0,0,0.35);
        border: 1px solid rgba(255,255,255,0.10);
        display:flex;
        align-items:center;
        justify-content:center;
        backdrop-filter: blur(10px);
      }}
      .lock {{
        font-size: 110px;
        line-height: 1;
        opacity: 0.9;
      }}
      .scen {{
        margin-top: 18px;
        font-size: 24px;
        color: rgba(255,255,255,0.92);
      }}
      .hint {{
        margin-top: 6px;
        color: rgba(255,255,255,0.55);
        font-size: 14px;
      }}
      .toast {{
        position:fixed;
        bottom: 18px;
        left: 50%;
        transform: translateX(-50%);
        background: rgba(0,0,0,0.75);
        border: 1px solid rgba(255,255,255,0.12);
        color: rgba(255,255,255,0.9);
        padding: 10px 14px;
        border-radius: 12px;
        backdrop-filter: blur(10px);
        max-width: min(820px, calc(100% - 24px));
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      @media (max-width: 820px) {{
        .center {{ gap: 18px; }}
        .sidebtn {{ width: 96px; }}
        .sidebtn .label {{ font-size: 18px; }}
        .ring {{ width: 260px; height: 260px; }}
        .ring .inner {{ width: 210px; height: 210px; }}
        .lock {{ font-size: 88px; }}
      }}
      @media (max-width: 560px) {{
        .center {{ flex-direction: column; gap: 14px; }}
        .sidebtn {{ width: auto; }}
        .sidebtn .label {{ font-size: 16px; }}
      }}
      .modal {{
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.72);
        backdrop-filter: blur(8px);
        display: none;
        align-items: center;
        justify-content: center;
        z-index: 5;
      }}
      .modal.show {{ display: flex; }}
      .sheet {{
        width: min(820px, calc(100% - 24px));
        max-height: min(640px, calc(100% - 24px));
        overflow: auto;
        background: rgba(14,16,22,0.95);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 16px;
        box-shadow: 0 22px 80px rgba(0,0,0,0.55);
      }}
      .sheetHead {{
        position: sticky;
        top: 0;
        background: rgba(14,16,22,0.92);
        backdrop-filter: blur(10px);
        padding: 14px 14px 10px;
        border-bottom: 1px solid rgba(255,255,255,0.08);
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 10px;
      }}
      .sheetTitle {{ font-size: 18px; color: rgba(255,255,255,0.92); }}
      .closeBtn {{
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(255,255,255,0.06);
        color: rgba(255,255,255,0.9);
        padding: 8px 12px;
        border-radius: 12px;
        cursor: pointer;
      }}
      .sheetBody {{ padding: 10px 14px 14px; }}
      .scFilter {{
        width: 100%;
        box-sizing: border-box;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(0,0,0,0.28);
        color: rgba(255,255,255,0.92);
        outline: none;
      }}
      .scList {{ margin-top: 10px; }}
      .scRow {{
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 10px;
        padding: 12px 12px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.08);
        background: rgba(255,255,255,0.04);
        margin-bottom: 10px;
      }}
      .scLeft {{ min-width: 0; }}
      .scName {{ font-size: 16px; color: rgba(255,255,255,0.92); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
      .scMeta {{ font-size: 12px; color: rgba(255,255,255,0.58); margin-top: 4px; }}
      .scBtn {{
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(255,255,255,0.06);
        color: rgba(255,255,255,0.9);
        padding: 8px 12px;
        border-radius: 12px;
        cursor: pointer;
        white-space: nowrap;
      }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="tab active" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab" href="/security/functions">Funzioni</a>
    </div>

    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="center">
        <div class="sidebtn" id="btnEmergency">
          <div class="icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <path d="M12 2.4l2.7 5.4 6 0.9-4.3 4.2 1 6-5.4-2.8-5.4 2.8 1-6L3.3 8.7l6-0.9L12 2.4z" stroke="rgba(255,255,255,0.85)" stroke-width="1.5" stroke-linejoin="round"/>
            </svg>
          </div>
          <div class="label">Preferiti</div>
        </div>

        <div class="main">
          <div class="statusline" id="statusLine">Scenario attivo</div>
          <div class="ring" id="ring" title="Cambia scenario">
            <div class="inner"><div class="lock" id="lockIcon">🔓</div></div>
          </div>
          <div class="scen" id="scenName">{_html_escape(scen_desc)}</div>
          <div class="hint" id="hint">Codice: {_html_escape(scen_code)} · tocchi il cerchio per cambiare scenario (work in progress)</div>
        </div>

        <div class="sidebtn" id="btnFunctions">
          <div class="icon">≡</div>
          <div class="label">Funzioni</div>
        </div>
      </div>
    </div>

    <div class="toast" id="toast">Pronto</div>

    <div class="modal" id="modal">
      <div class="sheet">
        <div class="sheetHead">
          <div class="sheetTitle">Scenari</div>
          <button class="closeBtn" id="closeBtn">Chiudi</button>
        </div>
        <div class="sheetBody">
          <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:10px;">
            <button class="scBtn" id="tabArm" type="button">Arm totale</button>
            <button class="scBtn" id="tabDisarm" type="button">Disarm</button>
            <button class="scBtn" id="tabPartial" type="button">Parziale</button>
            <button class="scBtn" id="tabHome" type="button">Smarthome</button>
            <button class="scBtn" id="tabAll" type="button">Tutti</button>
          </div>
          <input class="scFilter" id="scFilter" placeholder="Cerca scenario..." />
          <div class="scList" id="scList"></div>
        </div>
      </div>
    </div>

    <script>
      const toast = document.getElementById('toast');
      function setToast(msg) {{ toast.textContent = msg; }}

      const SCENARIOS = {json.dumps(scenarios, ensure_ascii=False)};

      function scenarioFromSystem(sysRt) {{
        const arm = (sysRt && sysRt.ARM) ? sysRt.ARM : null;
        if (!arm || typeof arm !== 'object') return {{desc:'Sconosciuto', code:'?'}};
        return {{desc: String(arm.D || 'Sconosciuto'), code: String(arm.S || '?')}};
      }}

      function isArmed(code) {{
        const c = String(code || '').toUpperCase();
        return !(c === 'D' || c === 'DISARM' || c === 'DISINSERITO');
      }}

      function applySystem(s) {{
        const scen = scenarioFromSystem(s);
        document.getElementById('scenName').textContent = scen.desc || 'Sconosciuto';
        const armed = isArmed(scen.code);
        document.getElementById('lockIcon').textContent = armed ? '🔒' : '🔓';
        const line = document.getElementById('statusLine');
        line.textContent = armed ? 'Sistema inserito' : 'Sistema disinserito';
      }}

      async function fetchSnap() {{
        try {{
          const res = await fetch('./api/entities', {{cache:'no-store'}});
          const snap = await res.json();
          const sys = (snap.entities || []).find(e => String(e.type||'') === 'systems');
          applySystem((sys && sys.realtime) ? sys.realtime : (sys && sys.static) ? sys.static : null);
        }} catch (e) {{
          setToast('Errore lettura stato: ' + e);
        }}
      }}

      // Live updates (SSE), fallback polling
      let sse = null;
      function startSSE() {{
        if (!window.EventSource) return false;
        try {{ sse = new EventSource('./api/stream'); }} catch (_e) {{ return false; }}
        sse.onmessage = (ev) => {{
          try {{
            const msg = JSON.parse(ev.data || '{{}}');
            const ents = msg.entities || msg.entities;
            const sys = (ents || []).find(e => String(e.type||'') === 'systems');
            if (sys) applySystem(sys.realtime || sys.static || null);
          }} catch (_e) {{}}
        }};
        sse.onerror = () => {{
          try {{ sse.close(); }} catch (_e) {{}}
          sse = null;
        }};
        return true;
      }}

      // Always keep a lightweight polling loop so the UI self-heals even if
      // realtime misses a critical transition (e.g., end of entry/exit delay).
      fetchSnap();
      startSSE();
      setInterval(fetchSnap, 2000);

      // Ensure UI catches up after sleep/backgrounding.
      document.addEventListener('visibilitychange', () => {{
        if (!document.hidden) fetchSnap();
      }});

      // Minimal command sender (reuses the same PIN session model as index_debug).
      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) {{
          throw new Error((data && data.error) ? data.error : txt);
        }}
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares (sessione temporanea):');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}
      async function sendCmd(type, id, action, value=null, _retry=false) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const token = getPinSessionToken();
        if (token) payload.token = token;
        try {{
          const res = await fetch('/api/cmd', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
          }});
          const text = await res.text();
          let data = null;
          try {{ data = JSON.parse(text); }} catch (_e) {{}}
          const errRaw = (data && data.error !== undefined) ? String(data.error) : '';
          const err = errRaw.trim().toLowerCase();
          const needsPin = (
            err === 'pin_session_required' ||
            err === 'invalid_token' ||
            err === 'login_failed' ||
            (data && data.ok === false && err === '')
          );
          if (needsPin && !_retry) {{
            try {{
              localStorage.removeItem(PIN_TOKEN_KEY);
              localStorage.removeItem(PIN_EXP_KEY);
            }} catch (_e) {{}}
            try {{
              await ensurePinSession();
            }} catch (e) {{
              const emsg = (e && e.message) ? e.message : String(e || 'PIN non valido');
              return {{ ok:false, text: emsg, data: {{ ok:false, error: emsg }} }};
            }}
            return await sendCmd(type, id, action, value, true);
          }}
          return {{ ok: res.ok, text, data }};
        }} catch (e) {{
          return {{ ok: false, text: String(e), data: null }};
        }}
      }}

      // Scenario modal
      const modal = document.getElementById('modal');
      const closeBtn = document.getElementById('closeBtn');
      const scFilter = document.getElementById('scFilter');
      const scList = document.getElementById('scList');
      const tabArm = document.getElementById('tabArm');
      const tabDisarm = document.getElementById('tabDisarm');
      const tabPartial = document.getElementById('tabPartial');
      const tabHome = document.getElementById('tabHome');
      const tabAll = document.getElementById('tabAll');
      let scTab = 'SEC_ARM';

      function openModal() {{
        modal.classList.add('show');
        scFilter.value = '';
        scTab = 'SEC_ARM';
        applyTabUi();
        renderScenarioList('');
        scFilter.focus();
      }}
      function closeModal() {{ modal.classList.remove('show'); }}

      function isSecurityCat(cat) {{
        const c = String(cat || '').toUpperCase();
        return (c === 'ARM' || c === 'DISARM' || c === 'PARTIAL');
      }}

      function applyTabUi() {{
        const on = (btn, enabled) => {{
          if (!btn) return;
          btn.style.opacity = enabled ? '1' : '0.7';
          btn.style.borderColor = enabled ? 'rgba(255,255,255,0.24)' : 'rgba(255,255,255,0.14)';
        }};
        on(tabArm, scTab === 'SEC_ARM');
        on(tabDisarm, scTab === 'SEC_DISARM');
        on(tabPartial, scTab === 'SEC_PARTIAL');
        on(tabHome, scTab === 'HOME');
        on(tabAll, scTab === 'ALL');
      }}

      function renderScenarioList(q) {{
        const needle = String(q||'').toLowerCase();
        let items = SCENARIOS;
        if (scTab === 'SEC_ARM') items = items.filter(s => String(s.CAT||'').toUpperCase() === 'ARM');
        if (scTab === 'SEC_DISARM') items = items.filter(s => String(s.CAT||'').toUpperCase() === 'DISARM');
        if (scTab === 'SEC_PARTIAL') items = items.filter(s => String(s.CAT||'').toUpperCase() === 'PARTIAL');
        if (scTab === 'HOME') items = items.filter(s => !isSecurityCat(s.CAT));
        items = items.filter(s => (String(s.DES||'') + ' ' + String(s.CAT||'') + ' ' + String(s.ID||'')).toLowerCase().includes(needle));
        scList.innerHTML = items.map(s => `
          <div class="scRow">
            <div class="scLeft">
              <div class="scName">${{escapeHtml(s.DES)}}</div>
              <div class="scMeta">ID ${{escapeHtml(String(s.ID))}} · ${{escapeHtml(String(s.CAT||''))}}</div>
            </div>
            <button class="scBtn" data-sid="${{escapeHtml(String(s.ID))}}">Esegui</button>
          </div>
        `).join('') || '<div class="muted">Nessuno scenario</div>';
        for (const btn of scList.querySelectorAll('button[data-sid]')) {{
          btn.addEventListener('click', async (ev) => {{
            const sid = Number(ev.currentTarget.getAttribute('data-sid'));
            setToast('Eseguo scenario ' + sid + '...');
            const res = await sendCmd('scenarios', sid, 'execute');
            setToast(res.ok ? 'OK' : ('Errore: ' + res.text));
            closeModal();
          }});
        }}
      }}

      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}

      document.getElementById('ring').addEventListener('click', () => openModal());
      const btnEmergency = document.getElementById('btnEmergency');
      if (btnEmergency) {{
        btnEmergency.addEventListener('click', () => {{ window.location.href = '/security/favorites'; }});
      }}
      const btnFunctions = document.getElementById('btnFunctions');
      if (btnFunctions) {{
        btnFunctions.addEventListener('click', () => {{ window.location.href = '/security/functions'; }});
      }}
      closeBtn.addEventListener('click', () => closeModal());
      modal.addEventListener('click', (ev) => {{ if (ev.target === modal) closeModal(); }});
      scFilter.addEventListener('input', () => renderScenarioList(scFilter.value));
      tabArm.addEventListener('click', () => {{ scTab = 'SEC_ARM'; applyTabUi(); renderScenarioList(scFilter.value); }});
      tabDisarm.addEventListener('click', () => {{ scTab = 'SEC_DISARM'; applyTabUi(); renderScenarioList(scFilter.value); }});
      tabPartial.addEventListener('click', () => {{ scTab = 'SEC_PARTIAL'; applyTabUi(); renderScenarioList(scFilter.value); }});
      tabHome.addEventListener('click', () => {{ scTab = 'HOME'; applyTabUi(); renderScenarioList(scFilter.value); }});
      tabAll.addEventListener('click', () => {{ scTab = 'ALL'; applyTabUi(); renderScenarioList(scFilter.value); }});
      applyTabUi();
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_sensors(snapshot):
    # All rendering happens client-side from /api/entities (live via SSE).
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Sensori</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
        --ok: #1ed760;
        --warn: #ffb020;
        --bad: #ff4d4d;
        --info: #39a0ff;
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        position: sticky;
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px 16px 48px; }}
      .controls {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom: 12px; }}
      .controls .right {{ display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-wrap:wrap; }}
      .chip {{ display:inline-flex; align-items:center; gap:8px; padding: 8px 12px; border:1px solid var(--border); border-radius: 999px; background: rgba(0,0,0,0.25); }}
      .chip.ok {{ border-color: rgba(30,215,96,0.30); color: rgba(30,215,96,0.95); }}
      .chip.bad {{ border-color: rgba(255,77,77,0.30); color: rgba(255,77,77,0.95); }}
      .btn {{ background: rgba(0,0,0,0.10); border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.85); border-radius: 999px; padding: 8px 12px; cursor:pointer; font-size: 14px; }}
      .btn.active {{ border-color: rgba(255,255,255,0.28); color: rgba(255,255,255,0.98); background: rgba(255,255,255,0.08); }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      .btn.mini {{ padding: 6px 10px; font-size: 12px; }}
      input[type="checkbox"] {{ width: 18px; height: 18px; }}
      .group {{ margin-top: 16px; }}
      .gtitle {{ color: rgba(255,255,255,0.75); font-size: 13px; letter-spacing: 1.2px; text-transform: uppercase; margin: 10px 2px; }}
      .list {{ border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 14px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .name {{ font-size: 18px; }}
      .sub {{ margin-top: 4px; color: rgba(255,255,255,0.65); font-size: 14px; }}
      .badges {{ display:flex; align-items:center; justify-content:flex-end; gap: 8px; flex-wrap: wrap; }}
      .badge {{ font-size: 12px; padding: 4px 8px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.12); color: rgba(255,255,255,0.8); }}
      .b-ok {{ border-color: rgba(30,215,96,0.35); color: rgba(30,215,96,0.95); }}
      .b-warn {{ border-color: rgba(255,176,32,0.45); color: rgba(255,176,32,0.95); }}
      .b-bad {{ border-color: rgba(255,77,77,0.45); color: rgba(255,77,77,0.95); }}
      .b-info {{ border-color: rgba(57,160,255,0.45); color: rgba(57,160,255,0.95); }}
      .muted {{ color: rgba(255,255,255,0.55); }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/scenarios">Scenari</a>
      <a class="tab active" href="/security/sensors">Sensori</a>
    </div>

      <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="controls">
        <div class="chip">
          <button class="btn" id="sortAZ">A-Z</button>
          <button class="btn" id="sortDate">Data</button>
          <button class="btn" id="sortType">Tipo</button>
        </div>
        <div class="chip" id="filterChip"></div>
        <div class="right">
          <div class="chip" id="ws1Status">Stato: -</div>
          <button class="btn mini" id="ws1Reconnect" style="display:none;">Riconnetti</button>
        </div>
      </div>

      <div id="content"></div>
    </div>
    <div class="toast" id="toast"></div>

    <script>
      const el = document.getElementById('content');
      const filterChip = document.getElementById('filterChip');
      const toastEl = document.getElementById('toast');
      const ws1Status = document.getElementById('ws1Status');
      const ws1Reconnect = document.getElementById('ws1Reconnect');
      const sortAZ = document.getElementById('sortAZ');
      const sortDate = document.getElementById('sortDate');
      const sortType = document.getElementById('sortType');
      const SORT_KEY = 'ksenia_security_sensors_sort';
      let sortMode = (localStorage.getItem(SORT_KEY) || 'AZ');
      function setSort(mode) {{
        sortMode = mode;
        try {{ localStorage.setItem(SORT_KEY, mode); }} catch (_e) {{}}
        if (sortAZ) sortAZ.classList.toggle('active', mode === 'AZ');
        if (sortDate) sortDate.classList.toggle('active', mode === 'DATE');
        if (sortType) sortType.classList.toggle('active', mode === 'TYPE');
        if (lastSnap) render(lastSnap);
      }}
      if (sortAZ) sortAZ.addEventListener('click', () => setSort('AZ'));
      if (sortDate) sortDate.addEventListener('click', () => setSort('DATE'));
      if (sortType) sortType.addEventListener('click', () => setSort('TYPE'));

      async function reconnectWs1() {{
        try {{
          if (ws1Reconnect) ws1Reconnect.disabled = true;
          toast('Riconnessione WS1...');
          const res = await fetch('/api/cmd', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ type: 'ws', action: 'reconnect_main' }}),
          }});
          const txt = await res.text();
          let data = null;
          try {{ data = JSON.parse(txt); }} catch (_e) {{}}
          toast((data && data.ok) ? 'OK' : ('Errore: ' + ((data && data.error) ? data.error : txt)));
          try {{ setTimeout(() => fetchSnap().then(s => {{ lastSnap = s; render(s); }}), 400); }} catch (_e) {{}}
        }} catch (e) {{
          toast('Errore: ' + String(e && e.message ? e.message : e));
        }} finally {{
          try {{ if (ws1Reconnect) ws1Reconnect.disabled = false; }} catch (_e) {{}}
        }}
      }}
      if (ws1Reconnect) ws1Reconnect.addEventListener('click', (ev) => {{ ev.preventDefault(); reconnectWs1(); }});

      let filterMode = 'ALL';
      function setFilter(mode) {{
        filterMode = String(mode || 'ALL').toUpperCase();
        try {{
          for (const b of document.querySelectorAll('button[data-filter]')) {{
            b.classList.toggle('active', String(b.getAttribute('data-filter') || '').toUpperCase() === filterMode);
          }}
        }} catch (_e) {{}}
        if (lastSnap) render(lastSnap);
      }}
      try {{
        if (filterChip) {{
          filterChip.innerHTML = `
            <button class="btn active" data-filter="ALL">Tutte</button>
            <button class="btn" data-filter="TROUBLE">Anomalie</button>
            <button class="btn" data-filter="ALARM">Allarme</button>
            <button class="btn" data-filter="EXCLUDED">Escluse</button>
            <button class="btn" data-filter="MASKED">Mascherate</button>
            <button class="btn" data-filter="TAMPER">Sabotaggio</button>
          `;
          for (const b of filterChip.querySelectorAll('button[data-filter]')) {{
            b.addEventListener('click', () => setFilter(b.getAttribute('data-filter')));
          }}
        }}
      }} catch (_e) {{}}

      function zoneStatus(rt) {{
        if (!rt || typeof rt !== 'object') return {{ badges:[{{level:'ok', text:'Sconosciuto'}}], flags:{{}} }};
        const norm = (v) => String(v ?? '').trim().toUpperCase();
        const sta = norm(rt.STA);
        const t = norm(rt.T);
        const vas = norm(rt.VAS);
        const byp = norm(rt.BYP);

        const alarm = (sta === 'A');
        const excluded = !!byp && byp !== 'NO' && byp !== '0' && byp !== 'OK' && (byp.startsWith('MAN') || byp.startsWith('AUTO'));
        const tamper = !!t && t !== '0' && t !== 'N' && t !== 'OK';
        const masked = !!vas && vas !== '0' && vas !== 'F' && vas !== 'N' && vas !== 'OK';

        const badges = [];
        if (alarm) badges.push({{level:'bad', text:'Allarme'}});
        if (excluded) badges.push({{level:'warn', text:'Esclusa'}});
        if (masked) badges.push({{level:'info', text:'Mascherata'}});
        if (tamper) badges.push({{level:'warn', text:'Sabotaggio'}});
        if (!badges.length) badges.push({{level:'ok', text:'Normale'}});
        return {{ badges, flags: {{ alarm, excluded, masked, tamper }} }};
      }}

      let toastTimer = null;
      function toast(msg, ms=2600) {{
        if (!toastEl) return;
        toastEl.textContent = String(msg || '');
        toastEl.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{
          toastEl.style.display = 'none';
        }}, Number(ms || 2600));
      }}

      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) {{
          throw new Error((data && data.error) ? data.error : txt);
        }}
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares (sessione temporanea):');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}

      async function sendCmd(type, id, action, value=null, _retry=false) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const token = getPinSessionToken();
        if (token) payload.token = token;
        let res, txt;
        try {{
          res = await fetch('/api/cmd', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
          }});
          txt = await res.text();
        }} catch (e) {{
          return {{ ok: false, status: 0, data: null, text: String(e) }};
        }}
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        const ok = res.ok && (!data || data.ok !== false);
        if (!ok && !_retry) {{
          const errRaw = (data && data.error !== undefined) ? String(data.error) : '';
          const err = errRaw.trim().toLowerCase();
          const needsPin = (
            err === 'pin_session_required' ||
            err === 'invalid_token' ||
            err === 'login_failed' ||
            (data && data.ok === false && err === '')
          );
          if (needsPin) {{
            try {{
              localStorage.removeItem(PIN_TOKEN_KEY);
              localStorage.removeItem(PIN_EXP_KEY);
            }} catch (_e) {{}}
            try {{
              await ensurePinSession();
            }} catch (e) {{
              const emsg = (e && e.message) ? e.message : String(e || 'PIN non valido');
              return {{ ok: false, status: res.status, data: {{ ok: false, error: emsg }}, text: emsg }};
            }}
            return await sendCmd(type, id, action, value, true);
          }}
        }}
        return {{ ok, status: res.status, data, text: txt }};
      }}

      function badgeClass(level) {{
        if (level === 'bad') return 'badge b-bad';
        if (level === 'warn') return 'badge b-warn';
        if (level === 'info') return 'badge b-info';
        return 'badge b-ok';
      }}

      function groupZones(snapshot) {{
        const ents = snapshot.entities || [];
        const parts = ents.filter(e => String(e.type||'').toLowerCase() === 'partitions');
        const zones = ents.filter(e => String(e.type||'').toLowerCase() === 'zones');

        const partById = new Map();
        for (const p of parts) {{
          const id = String(p.id);
          const st = (p.static && typeof p.static === 'object') ? p.static : {{}};
          const name = String(p.name || st.DES || ('Partizione ' + id));
          partById.set(id, name);
        }}

        const maxPid = (() => {{
          let m = 0;
          for (const k of partById.keys()) {{
            const n = Number(k);
            if (Number.isFinite(n) && n > m) m = n;
          }}
          return Math.max(m, 20);
        }})();

        function decodeMask(maskNum) {{
          const out = [];
          for (let pid = 1; pid <= maxPid; pid++) {{
            const bit = 1 << (pid - 1);
            if ((maskNum & bit) === bit) out.push(pid);
          }}
          return out;
        }}

        function decodeZonePrt(value) {{
          const raw = (value === null || value === undefined) ? '' : String(value).trim();
          const up = raw.toUpperCase();
          if (up === 'ALL' || up === 'ALLALL') {{
            return Array.from(partById.keys()).map(k => Number(k)).filter(n => Number.isFinite(n)).sort((a,b) => a-b);
          }}
          const candidates = [];
          // decimal parse
          const dec = Number(raw.replace(',', '.'));
          if (Number.isFinite(dec) && dec >= 0) candidates.push({{fmt:'dec', m: Math.trunc(dec)}});
          // hex parse (heuristic)
          const hexish = raw.length >= 2 && /^[0-9a-fA-F]+$/.test(raw);
          if (up.startsWith('0X')) {{
            const hx = parseInt(raw, 16);
            if (Number.isFinite(hx) && hx >= 0) candidates.push({{fmt:'hex', m: hx}});
          }} else if (hexish) {{
            const hx = parseInt(raw, 16);
            if (Number.isFinite(hx) && hx >= 0) candidates.push({{fmt:'hex', m: hx}});
          }}

          const scored = [];
          for (const c of candidates) {{
            const decoded = decodeMask(c.m);
            if (!decoded.length) continue;
            scored.push({{len: decoded.length, pref: c.fmt === 'hex' ? 0 : 1, decoded}});
          }}
          if (!scored.length) return [];
          scored.sort((a,b) => (a.len - b.len) || (a.pref - b.pref));
          return scored[0].decoded;
        }}

        const flat = [];

        for (const z of zones) {{
          const st = z.static || {{}};
          const rt = z.realtime || {{}};
          const pids = decodeZonePrt(st.PRT);
          const partIds = pids.map(n => String(n));
          const pn = partIds.map(pid => partById.get(pid) || ('Partizione ' + pid)).filter(Boolean);

          flat.push({{
            id: String(z.id),
            name: String(z.name || st.DES || ('Zona ' + String(z.id))),
            cat: String(st.CAT || ''),
            parts: pn,
            lastSeen: Number(z.last_seen || 0),
            rt: rt,
            st: st,
          }});
        }}

        return {{ flat }};
      }}

      function getPanelTimeInfo(snapshot) {{
        try {{
          const ents = (snapshot && snapshot.entities) ? snapshot.entities : [];
          const sys = (ents || []).find(e => String(e.type || '').toLowerCase() === 'systems') || null;
          const rt = (sys && sys.realtime && typeof sys.realtime === 'object') ? sys.realtime : {{}};
          const st = (sys && sys.static && typeof sys.static === 'object') ? sys.static : {{}};
          const timeObj = (rt.TIME && typeof rt.TIME === 'object') ? rt.TIME : ((st.TIME && typeof st.TIME === 'object') ? st.TIME : null);
          const gmt = timeObj ? Number(timeObj.GMT ?? timeObj.gmt ?? 0) : NaN;
          const tzm = timeObj ? Number(timeObj.TZM ?? timeObj.tzm ?? NaN) : NaN;
          const tz = timeObj ? Number(timeObj.TZ ?? timeObj.tz ?? NaN) : NaN;
          const offsetMin = Number.isFinite(tzm) ? tzm : (Number.isFinite(tz) ? (tz * 60) : 0);
          return {{ gmt, offsetMin }};
        }} catch (_e) {{
          return {{ gmt: NaN, offsetMin: 0 }};
        }}
      }}

      function fmtPanelTs(epochSec, offsetMin) {{
        const n = Number(epochSec || 0);
        const off = Number(offsetMin || 0);
        if (!Number.isFinite(n)) return '';
        const d = new Date((n + (off * 60)) * 1000);
        const pad = (x) => String(x).padStart(2, '0');
        return `${{d.getUTCFullYear()}}-${{pad(d.getUTCMonth()+1)}}-${{pad(d.getUTCDate())}} ${{pad(d.getUTCHours())}}:${{pad(d.getUTCMinutes())}}:${{pad(d.getUTCSeconds())}}`;
      }}

      function render(snapshot) {{
        const pt = getPanelTimeInfo(snapshot);
        const fallbackOffsetMin = -new Date().getTimezoneOffset();
        const offsetMin = (pt && Number.isFinite(pt.offsetMin)) ? Number(pt.offsetMin) : fallbackOffsetMin;
        const {{ flat }} = groupZones(snapshot);

        // New view: single list with sorting (A-Z / Data / Tipo)
        let list = (flat || []).slice();
        if (filterMode !== 'ALL') {{
          list = list.filter(x => {{
            const zs = zoneStatus(x.rt);
            const f = (zs && zs.flags) ? zs.flags : {{}};
            if (filterMode === 'ALARM') return !!f.alarm;
            if (filterMode === 'EXCLUDED') return !!f.excluded;
            if (filterMode === 'MASKED') return !!f.masked;
            if (filterMode === 'TAMPER') return !!f.tamper;
            if (filterMode === 'TROUBLE') return !!(f.alarm || f.excluded || f.masked || f.tamper);
            return true;
          }});
        }}

        const sev = (lvl) => (lvl === 'bad' ? 0 : lvl === 'warn' ? 1 : lvl === 'info' ? 2 : 3);
        if (sortMode === 'AZ') {{
          list.sort((a,b) => a.name.localeCompare(b.name, 'it', {{sensitivity:'base'}}));
        }} else if (sortMode === 'TYPE') {{
          list.sort((a,b) => (a.cat||'').localeCompare(b.cat||'', 'it', {{sensitivity:'base'}}) || a.name.localeCompare(b.name, 'it', {{sensitivity:'base'}}));
        }} else {{
          // DATE: last event first
          list.sort((a,b) => (Number(b.lastSeen||0) - Number(a.lastSeen||0)) || a.name.localeCompare(b.name, 'it', {{sensitivity:'base'}}));
        }}

        const out = [];
        out.push('<div class="group"><div class="gtitle">Sensori</div><div class="list">');
        for (const z of list) {{
          const zs = zoneStatus(z.rt);
          const badges = (zs && zs.badges) ? zs.badges : [{{level:'ok', text:'Normale'}}];
          const byp = String(z.rt.BYP||'').toUpperCase();
          const parts = (z.parts && z.parts.length) ? z.parts.join(', ') : '-';
          const when = z.lastSeen ? fmtPanelTs(z.lastSeen, offsetMin) : '';
          const sub = `${{parts}}${{z.cat ? (" - " + z.cat) : ""}}${{when ? (" - " + when) : ""}}${{(byp && byp !== "NO") ? (" - Esclusa: " + byp) : ""}}`;
          const f = (zs && zs.flags) ? zs.flags : {{}};
          const btnLabel = f.excluded ? 'Includi' : 'Escludi';
          const btnAction = f.excluded ? 'byp_off' : 'byp_on';
          const btnHtml = `<button class="btn mini" data-zid="${{escapeHtml(String(z.id))}}" data-act="${{escapeHtml(btnAction)}}">${{escapeHtml(btnLabel)}}</button>`;
          out.push(`<div class="row"><div><div class="name">${{escapeHtml(z.name)}}</div><div class="sub">${{escapeHtml(sub)}}</div></div><div class="badges">${{badges.map(b => `<span class="${{badgeClass(b.level)}}">${{escapeHtml(b.text)}}</span>`).join('')}}${{btnHtml}}</div></div>`);
        }}
        out.push('</div></div>');
        el.innerHTML = out.join('');
        try {{
          for (const b of el.querySelectorAll('button[data-zid][data-act]')) {{
            b.addEventListener('click', async (ev) => {{
              ev.preventDefault();
              const btn = ev.currentTarget;
              const zid = Number(btn.getAttribute('data-zid') || '0');
              const act = String(btn.getAttribute('data-act') || '');
              if (!zid || !act) return;
              btn.disabled = true;
              toast((act === 'byp_on') ? ('Escludo zona ' + zid + '...') : ('Includo zona ' + zid + '...'));
              const res = await sendCmd('zones', zid, act);
              toast(res.ok ? 'OK' : ('Errore: ' + (res.data && res.data.error ? res.data.error : res.text)));
              try {{ const s = await fetchSnap(); lastSnap = s; render(s); }} catch (_e) {{}}
              btn.disabled = false;
            }});
          }}
        }} catch (_e) {{}}
        const wsOk = !!(snapshot.meta && snapshot.meta.ws1_connected);
        if (ws1Status) {{
          ws1Status.textContent = wsOk ? 'Stato: OK' : 'Stato: ERRORE';
          ws1Status.classList.toggle('ok', wsOk);
          ws1Status.classList.toggle('bad', !wsOk);
        }}
        if (ws1Reconnect) ws1Reconnect.style.display = wsOk ? 'none' : 'inline-flex';
      }}

      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}

      async function fetchSnap() {{
        const res = await fetch('/api/entities', {{cache:'no-store'}});
        return await res.json();
      }}

      let lastSnap = null;
      // removed: onlyAlarm checkbox (replaced by filter buttons)
      setSort(sortMode);

      // Live updates (SSE), fallback polling
      let sse = null;
      let pollTimer = null;
      function startPolling() {{
        if (pollTimer) return;
        pollTimer = setInterval(() => fetchSnap().then(s => {{ lastSnap = s; render(s); }}), 5000);
      }}
      function startSSE() {{
        if (!window.EventSource) return false;
        try {{ sse = new EventSource('/api/stream'); }} catch (_e) {{ return false; }}
        sse.onmessage = (ev) => {{
          try {{
            const msg = JSON.parse(ev.data || '{{}}');
            if (msg && msg.entities && msg.type !== 'update') {{
              lastSnap = {{
                entities: msg.entities || [],
                meta: msg.meta || {{}},
              }};
              render(lastSnap);
              return;
            }}
            if (msg && msg.type === 'update') {{
              // Just refetch full snapshot; simpler for this view.
              fetchSnap().then(s => {{ lastSnap = s; render(s); }});
              return;
            }}
          }} catch (_e) {{}}
        }};
        sse.onerror = () => {{
          try {{ sse.close(); }} catch (_e) {{}}
          sse = null;
          startPolling();
        }};
        return true;
      }}

      // Always keep a polling fallback so UI self-recovers after addon restarts.
      startPolling();
      fetchSnap().then(s => {{ lastSnap = s; render(s); }});
      if (!startSSE()) {{
        startPolling();
      }}

    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_partitions(snapshot):
    # All rendering happens client-side from /api/entities (live via SSE).
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Partizioni</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
        --ok: #1ed760;
        --warn: #ffb020;
        --bad: #ff4d4d;
        --info: #39a0ff;
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        position: sticky;
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px 16px 48px; }}
      .controls {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom: 12px; }}
      .controls .left {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
      .chip {{ display:inline-flex; align-items:center; gap:8px; padding: 8px 12px; border:1px solid var(--border); border-radius: 999px; background: rgba(0,0,0,0.25); }}
      .chip.ok {{ border-color: rgba(30,215,96,0.30); color: rgba(30,215,96,0.95); }}
      .chip.bad {{ border-color: rgba(255,77,77,0.30); color: rgba(255,77,77,0.95); }}
      .btn {{ background: rgba(0,0,0,0.10); border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.85); border-radius: 999px; padding: 8px 12px; cursor:pointer; font-size: 14px; }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      .btn.mini {{ padding: 6px 10px; font-size: 12px; }}
      .actions {{ display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap: wrap; }}
      input[type="checkbox"] {{ width: 18px; height: 18px; }}
      .group {{ margin-top: 16px; }}
      .gtitle {{ color: rgba(255,255,255,0.75); font-size: 13px; letter-spacing: 1.2px; text-transform: uppercase; margin: 10px 2px; }}
      .list {{ border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 14px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .name {{ font-size: 18px; }}
      .sub {{ margin-top: 4px; color: rgba(255,255,255,0.65); font-size: 14px; }}
      .badge {{ font-size: 12px; padding: 4px 8px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.12); color: rgba(255,255,255,0.8); white-space: nowrap; }}
      .b-ok {{ border-color: rgba(30,215,96,0.35); color: rgba(30,215,96,0.95); }}
      .b-warn {{ border-color: rgba(255,176,32,0.35); color: rgba(255,176,32,0.95); }}
      .b-bad {{ border-color: rgba(255,77,77,0.35); color: rgba(255,77,77,0.95); }}
      .b-info {{ border-color: rgba(57,160,255,0.35); color: rgba(57,160,255,0.95); }}
      .zlist {{ margin-top: 10px; display:flex; flex-direction: column; gap: 10px; }}
      .zrow {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 12px 12px; border:1px solid rgba(255,255,255,0.08); border-radius: 12px; background: rgba(0,0,0,0.18); }}
      .zname {{ font-size: 16px; }}
      .partToggle {{ all: unset; cursor: pointer; color: var(--fg); font-size: 18px; font-weight: 700; }}
      .partToggle:hover {{ text-decoration: underline; }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}

      @media (max-width: 720px) {{
        .controls {{ flex-direction: column; align-items: stretch; }}
      }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab active" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/sensors">Sensori</a>
    </div>
    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="controls">
        <div class="left">
          <div class="chip" id="ws1Status">Stato: -</div>
          <button class="btn mini" id="ws1Reconnect" style="display:none;">Riconnetti</button>
          <button class="btn mini" id="toggleZones" type="button">Espandi/Comprimi</button>
        </div>
        <div class="chip" id="lastUp">-</div>
      </div>
      <div id="out"></div>
    </div>
    <div class="toast" id="toast"></div>

    <script>
      const outEl = document.getElementById('out');
      const lastUp = document.getElementById('lastUp');
      const toastEl = document.getElementById('toast');
      const ws1Status = document.getElementById('ws1Status');
      const ws1Reconnect = document.getElementById('ws1Reconnect');
      const toggleZonesBtn = document.getElementById('toggleZones');
      if (lastUp) lastUp.textContent = 'Aggiornato: -';
      if (ws1Status) ws1Status.textContent = 'Stato: -';

      const expandedPids = new Set();

      async function reconnectWs1() {{
        try {{
          if (ws1Reconnect) ws1Reconnect.disabled = true;
          toast('Riconnessione WS1...');
          const res = await fetch('/api/cmd', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ type: 'ws', action: 'reconnect_main' }}),
          }});
          const txt = await res.text();
          let data = null;
          try {{ data = JSON.parse(txt); }} catch (_e) {{}}
          toast((data && data.ok) ? 'OK' : ('Errore: ' + ((data && data.error) ? data.error : txt)));
          try {{ setTimeout(fetchSnap, 400); }} catch (_e) {{}}
        }} catch (e) {{
          toast('Errore: ' + String(e && e.message ? e.message : e));
        }} finally {{
          try {{ if (ws1Reconnect) ws1Reconnect.disabled = false; }} catch (_e) {{}}
        }}
      }}
      if (ws1Reconnect) ws1Reconnect.addEventListener('click', (ev) => {{ ev.preventDefault(); reconnectWs1(); }});

      let toastTimer = null;
      function toast(msg, ms=2600) {{
        if (!toastEl) return;
        toastEl.textContent = String(msg || '');
        toastEl.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{
          toastEl.style.display = 'none';
        }}, Number(ms || 2600));
      }}

      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) {{
          throw new Error((data && data.error) ? data.error : txt);
        }}
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares (sessione temporanea):');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}

      async function sendCmd(type, id, action, value=null, _retry=false) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const token = getPinSessionToken();
        if (token) payload.token = token;
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        const ok = res.ok && (!data || data.ok !== false);
        if (!ok && !_retry) {{
          const errRaw = (data && data.error !== undefined) ? String(data.error) : '';
          const err = errRaw.trim().toLowerCase();
          const needsPin = (
            err === 'pin_session_required' ||
            err === 'invalid_token' ||
            err === 'login_failed' ||
            (data && data.ok === false && err === '')
          );
          if (needsPin) {{
            try {{ localStorage.removeItem(PIN_TOKEN_KEY); localStorage.removeItem(PIN_EXP_KEY); }} catch (_e) {{}}
            try {{
              await ensurePinSession();
            }} catch (e) {{
              const emsg = (e && e.message) ? e.message : String(e || 'PIN non valido');
              return {{ ok: false, data: {{ ok: false, error: emsg }}, text: emsg }};
            }}
            return await sendCmd(type, id, action, value, true);
          }}
        }}
        return {{ ok, data, text: txt }};
      }}

      function badgeClass(level) {{
        if (level === 'bad') return 'badge b-bad';
        if (level === 'warn') return 'badge b-warn';
        if (level === 'info') return 'badge b-info';
        return 'badge b-ok';
      }}

      function zoneStatus(rt) {{
        if (!rt || typeof rt !== 'object') return {{ badges:[{{level:'ok', text:'Sconosciuto'}}], flags:{{}} }};
        const norm = (v) => String(v ?? '').trim().toUpperCase();
        const sta = norm(rt.STA);
        const t = norm(rt.T);
        const vas = norm(rt.VAS);
        const byp = norm(rt.BYP);

        const alarm = (sta === 'A');
        const excluded = !!byp && byp !== 'NO' && byp !== '0' && byp !== 'OK' && (byp.startsWith('MAN') || byp.startsWith('AUTO'));
        const tamper = !!t && t !== '0' && t !== 'N' && t !== 'OK';
        const masked = !!vas && vas !== '0' && vas !== 'F' && vas !== 'N' && vas !== 'OK';

        const badges = [];
        if (alarm) badges.push({{level:'bad', text:'Allarme'}});
        if (excluded) badges.push({{level:'warn', text:'Esclusa'}});
        if (masked) badges.push({{level:'info', text:'Mascherata'}});
        if (tamper) badges.push({{level:'warn', text:'Sabotaggio'}});
        if (!badges.length) badges.push({{level:'ok', text:'Normale'}});
        return {{ badges, flags: {{ alarm, excluded, masked, tamper }} }};
      }}

      function partitionState(rt) {{
        if (!rt || typeof rt !== 'object') return {{level:'ok', text:'Sconosciuta', armed:false}};
        const armObj = rt.ARM;
        const arm = (armObj && typeof armObj === 'object')
          ? String(armObj.S || armObj.s || armObj.CODE || '')
          : String(armObj || '');
        const armUp = arm.trim().toUpperCase();
        const ast = String(rt.AST || '').trim().toUpperCase();
        const disarmed = (!armUp) || (armUp === 'D' || armUp === 'DISARM' || armUp === 'DISINSERITO');
        const armed = !disarmed;
        if (!armed && (!ast || ast === 'OK')) return {{level:'ok', text:'Disinserita', armed:false, mode:'D'}};

        const mod = String(rt.MOD || '').trim().toUpperCase();
        // Your panel uses IA/DA as realtime codes. Accept both IA/I and DA/A.
        const instant = (mod === 'IA' || mod === 'I') || (armUp === 'IA' || armUp.startsWith('IA') || armUp === 'I');
        const delayed = (mod === 'DA' || mod === 'A') || (armUp === 'DA' || armUp.startsWith('DA') || armUp === 'A');
        const delayActive = (() => {{
          const ed = Number(rt.EXIT_DELAY || 0);
          const id = Number(rt.ENTRY_DELAY || 0);
          const t = String((rt.T !== undefined && rt.T !== null) ? rt.T : (rt.TIME !== undefined && rt.TIME !== null) ? rt.TIME : '').trim();
          if ((Number.isFinite(ed) && ed > 0) || (Number.isFinite(id) && id > 0)) return true;
          return !!t && t !== '0' && t !== '0:00' && t !== '0.00';
        }})();

        if (armed && (!ast || ast === 'OK')) {{
          if (instant) return {{level:'bad', text:'Inserita istantanea', armed:true, mode:'IA'}};
          if (delayed) return {{level:'warn', text:'Inserita (ritardata)', armed:true, mode:'DA'}};
          // Fallback: show armed as delayed by default.
          return {{level:'warn', text: delayActive ? 'Inserita (ritardata)' : 'Inserita (ritardata)', armed:true, mode:'DA'}};
        }}
        if (ast && ast !== 'OK') return {{level:'warn', text:'Attenzione', armed:armed, mode: instant ? 'IA' : 'DA'}};
        return {{level:'ok', text:'', armed:armed, mode: instant ? 'IA' : (armed ? 'DA' : 'D')}};
      }}

      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}

      function group(snapshot) {{
        const ents = snapshot.entities || [];
        const parts = ents.filter(e => String(e.type||'').toLowerCase() === 'partitions');
        const zones = ents.filter(e => String(e.type||'').toLowerCase() === 'zones');

        const partById = new Map();
        for (const p of parts) {{
          const id = String(p.id);
          const st = (p.static && typeof p.static === 'object') ? p.static : {{}};
          const name = String(p.name || st.DES || ('Partizione ' + id));
          partById.set(id, name);
        }}

        const maxPid = (() => {{
          let m = 0;
          for (const k of partById.keys()) {{
            const n = Number(k);
            if (Number.isFinite(n) && n > m) m = n;
          }}
          return Math.max(m, 20);
        }})();

        function decodeMask(maskNum) {{
          const out = [];
          for (let pid = 1; pid <= maxPid; pid++) {{
            const bit = 1 << (pid - 1);
            if ((maskNum & bit) === bit) out.push(pid);
          }}
          return out;
        }}

        function decodeZonePrt(value) {{
          const raw = (value === null || value === undefined) ? '' : String(value).trim();
          const up = raw.toUpperCase();
          if (up === 'ALL' || up === 'ALLALL') {{
            return Array.from(partById.keys()).map(k => Number(k)).filter(n => Number.isFinite(n)).sort((a,b) => a-b);
          }}
          const candidates = [];
          const dec = Number(raw.replace(',', '.'));
          if (Number.isFinite(dec) && dec >= 0) candidates.push({{fmt:'dec', m: Math.trunc(dec)}});
          const hexish = raw.length >= 2 && /^[0-9a-fA-F]+$/.test(raw);
          if (up.startsWith('0X')) {{
            const hx = parseInt(raw, 16);
            if (Number.isFinite(hx) && hx >= 0) candidates.push({{fmt:'hex', m: hx}});
          }} else if (hexish) {{
            const hx = parseInt(raw, 16);
            if (Number.isFinite(hx) && hx >= 0) candidates.push({{fmt:'hex', m: hx}});
          }}

          const scored = [];
          for (const c of candidates) {{
            const decoded = decodeMask(c.m);
            if (!decoded.length) continue;
            scored.push({{len: decoded.length, pref: c.fmt === 'hex' ? 0 : 1, decoded}});
          }}
          if (!scored.length) return [];
          scored.sort((a,b) => (a.len - b.len) || (a.pref - b.pref));
          return scored[0].decoded;
        }}

        const zonesByPid = new Map();
        for (const z of zones) {{
          const st = z.static || {{}};
          const rt = z.realtime || {{}};
          const pids = decodeZonePrt(st.PRT);
          const entry = {{
            id: String(z.id),
            name: String(z.name || st.DES || ('Zona ' + String(z.id))),
            rt: rt,
            st: st,
          }};
          for (const pidNum of pids) {{
            const pid = String(pidNum);
            if (!zonesByPid.has(pid)) zonesByPid.set(pid, []);
            zonesByPid.get(pid).push(entry);
          }}
        }}

        return {{ parts, partById, zonesByPid }};
      }}

      function render(snapshot) {{
        const {{ parts, partById, zonesByPid }} = group(snapshot);

        const sorted = parts.slice().sort((a,b) => (Number(a.id)||0) - (Number(b.id)||0));
        const blocks = [];
        for (const p of sorted) {{
          const pid = String(p.id);
          const name = partById.get(pid) || ('Partizione ' + pid);
          const rt = p.realtime || {{}};
          const st = p.static || {{}};
          const ps = partitionState(rt);
          const zlist = (zonesByPid.get(pid) || []).slice().sort((a,b) => a.name.localeCompare(b.name, 'it', {{sensitivity:'base'}}));

          const t = (rt.T !== undefined && rt.T !== null) ? String(rt.T) : (rt.TIME !== undefined && rt.TIME !== null) ? String(rt.TIME) : '';
          const extra = (t && t !== '0' && t !== '0:00' && t !== '0.00') ? ('Countdown: ' + t) : '';
          const btns = ps.armed
            ? `<button class="btn mini" data-pid="${{escapeHtml(pid)}}" data-act="disarm" type="button">Disinserisci</button>`
            : `<button class="btn mini" data-pid="${{escapeHtml(pid)}}" data-act="arm_delay" type="button">Inserisci</button>
               <button class="btn mini" data-pid="${{escapeHtml(pid)}}" data-act="arm_instant" type="button">Istantanea</button>`;
          const expanded = expandedPids.has(pid);
          const arrow = expanded ? '▾' : '▸';

          blocks.push(`<div class="group">
             
            <div class="list">
              <div class="row">
                <div>
                  <div class="name"><button class="partToggle" type="button" data-toggle-pid="${{escapeHtml(pid)}}">${{escapeHtml(arrow + ' ' + name)}}</button></div>
                  <div class="sub">${{extra ? escapeHtml(extra) : ''}}</div>
                </div>
                <div class="actions">
                  <span class="${{badgeClass(ps.level)}}">${{escapeHtml(ps.text)}}</span>
                  ${{btns}}
                </div>
              </div>
              ${{
                expanded
                  ? `<div class="row" style="align-items:flex-start;">
                       <div style="flex:1;">
                         <div class="sub">Zone associate (${{zlist.length}})</div>
                         <div class="zlist">
                           ${{
                             zlist.map(z => {{
                               const zs = (zoneStatus(z.rt).badges || [{{level:'ok', text:'Normale'}}])[0];
                               return `<div class="zrow">
                                 <div class="zname">${{escapeHtml(z.name)}}</div>
                                 <span class="${{badgeClass(zs.level)}}">${{escapeHtml(zs.text)}}</span>
                               </div>`;
                             }}).join('') || `<div class="sub" style="padding:6px 0;">Nessuna zona associata</div>`
                           }}
                         </div>
                       </div>
                     </div>`
                  : ''
              }}
            </div>
          </div>`);
        }}

        outEl.innerHTML = blocks.join('') || '<div class="group"><div class="gtitle">Nessuna partizione</div></div>';
        const lu = snapshot.meta && snapshot.meta.last_update ? new Date(snapshot.meta.last_update * 1000) : null;
        const ts = lu ? lu.toLocaleTimeString('it-IT') : new Date().toLocaleTimeString('it-IT');
        if (lastUp) lastUp.textContent = `Aggiornato: ${{ts}} | Partizioni: ${{parts.length}}`;

        const wsOk = !!(snapshot.meta && snapshot.meta.ws1_connected);
        if (ws1Status) {{
          ws1Status.textContent = wsOk ? 'Stato: OK' : 'Stato: ERRORE';
          ws1Status.classList.toggle('ok', wsOk);
          ws1Status.classList.toggle('bad', !wsOk);
        }}
        if (ws1Reconnect) ws1Reconnect.style.display = wsOk ? 'none' : 'inline-flex';
      }}

      async function fetchSnap() {{
        try {{
          const res = await fetch('/api/entities', {{cache:'no-store'}});
          const snap = await res.json();
          lastSnap = snap;
          render(snap);
        }} catch (_e) {{
          if (lastUp) lastUp.textContent = 'Errore aggiornamento';
        }}
      }}

      let lastSnap = null;
      let sse = null;
      function startSSE() {{
        if (!window.EventSource) return false;
        try {{ sse = new EventSource('/api/stream'); }} catch (_e) {{ return false; }}
        sse.onmessage = (ev) => {{
          try {{
            const msg = JSON.parse(ev.data || '{{}}');
            if (msg && msg.type === 'update') {{
              fetchSnap();
              return;
            }}
          }} catch (_e) {{}}
        }};
        sse.onerror = () => {{
          try {{ if (sse) sse.close(); }} catch (_e) {{}}
          sse = null;
        }};
        return true;
      }}

      fetchSnap();
      startSSE();
      setInterval(fetchSnap, 2000);
      document.addEventListener('visibilitychange', () => {{ if (!document.hidden) fetchSnap(); }});
      if (toggleZonesBtn) toggleZonesBtn.addEventListener('click', () => {{
        if (!lastSnap || !lastSnap.entities) return;
        const pids = (lastSnap.entities || [])
          .filter(e => String(e.type||'').toLowerCase() === 'partitions')
          .map(e => String(e.id));
        const allExpanded = pids.length > 0 && pids.every(pid => expandedPids.has(pid));
        expandedPids.clear();
        if (!allExpanded) {{
          for (const pid of pids) expandedPids.add(pid);
        }}
        render(lastSnap);
      }});

      outEl.addEventListener('click', async (ev) => {{
        const tog = ev.target && ev.target.closest ? ev.target.closest('button[data-toggle-pid]') : null;
        if (tog) {{
          ev.preventDefault();
          const pid = String(tog.getAttribute('data-toggle-pid') || '');
          if (pid) {{
            if (expandedPids.has(pid)) expandedPids.delete(pid);
            else expandedPids.add(pid);
            if (lastSnap) render(lastSnap);
          }}
          return;
        }}
        const btn = ev.target && ev.target.closest ? ev.target.closest('button[data-pid][data-act]') : null;
        if (!btn) return;
        ev.preventDefault();
        const pid = Number(btn.getAttribute('data-pid') || '0');
        const act = String(btn.getAttribute('data-act') || '');
        if (!pid || !act) return;
        try {{
          btn.disabled = true;
          await ensurePinSession();
          toast('Invio comando...');
          const res = await sendCmd('partitions', pid, act);
          toast(res.ok ? 'OK' : ('Errore: ' + ((res.data && res.data.error) ? res.data.error : res.text)));
          try {{ setTimeout(fetchSnap, 250); }} catch (_e) {{}}
        }} catch (e) {{
          toast('Errore: ' + String(e && e.message ? e.message : e));
        }} finally {{
          try {{ btn.disabled = false; }} catch (_e) {{}}
        }}
      }});
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_functions_all(snapshot):
    entities = snapshot.get("entities") or []
    ui_tags = _load_ui_tags()
    scenarios = []
    for e in entities:
        if str(e.get("type") or "").lower() != "scenarios":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        try:
            sid = int(e.get("id"))
        except Exception:
            continue
        cat = str(st.get("CAT") or "").strip().upper()
        if cat in ("ARM", "DISARM", "PARTIAL"):
            continue
        _tag, visible = _get_ui_tag(ui_tags, "scenarios", sid)
        if not visible:
            continue
        scenarios.append(
            {
                "ID": sid,
                "DES": st.get("DES") or e.get("name") or f"Scenario {sid}",
                "CAT": cat or "SMARTHOME",
            }
        )
    scenarios.sort(key=lambda x: (str(x.get("DES") or ""), str(x.get("ID") or "")))

    thermostats = []
    for e in entities:
        if str(e.get("type") or "").lower() != "thermostats":
            continue
        tid = str(e.get("id") or "")
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        name = e.get("name") or st.get("DES") or f"Termostato {tid}"
        thermostats.append({"ID": tid, "DES": name})
    thermostats.sort(key=lambda x: str(x.get("DES") or ""))

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Funzioni</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --card: rgba(0,0,0,0.30);
        --card2: rgba(255,255,255,0.04);
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{
        max-width: 1120px;
        margin: 0 auto;
        padding: 90px 18px 32px;
      }}
      .grid {{
        display:grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 16px;
      }}
      .card {{
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 14px 16px;
      }}
      .card h3 {{
        margin: 0 0 8px 0;
        font-size: 18px;
      }}
      .card .muted {{ color: var(--muted); font-size: 13px; }}
      .btn {{
        display:inline-flex;
        align-items:center;
        gap:8px;
        padding: 8px 12px;
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.14);
        background: var(--card2);
        color: var(--fg);
        cursor: pointer;
        text-decoration: none;
      }}
      .btn:hover {{ border-color: rgba(255,255,255,0.28); }}
      .list {{
        margin-top: 10px;
        display:flex;
        flex-direction: column;
        gap: 10px;
      }}
      .row {{
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 10px;
        padding: 10px 12px;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        background: rgba(255,255,255,0.04);
      }}
      .row .name {{ font-size: 15px; }}
      .row .meta {{ color: var(--muted); font-size: 12px; }}
      .row .actions {{ display:flex; gap:8px; }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/functions">Funzioni</a>
    </div>

    <div class="wrap">
       <div class="panel" id="panelSchedule">
        <div class="card" id="scenarios">
          <h3>Scenari smarthome</h3>
          <div class="muted">Esegui scenari non di sicurezza.</div>
          <div class="list" id="scenarioList">
            {''.join([
              f'<div class="row"><div><div class="name">{_html_escape(s["DES"])}</div><div class="meta">ID {_html_escape(str(s["ID"]))} · {_html_escape(s["CAT"])}</div></div><div class="actions"><button class="btn" data-sid="{_html_escape(str(s["ID"]))}">Esegui</button></div></div>'
              for s in scenarios
            ]) or '<div class="row"><div class="name">Nessuno scenario</div></div>'}
          </div>
        </div>

        <div class="card">
          <h3>Termostati</h3>
          <div class="muted">Accesso rapido ai dettagli.</div>
          <div class="list" id="thermostatList">
            {''.join([
              f'<div class="row"><div><div class="name">{_html_escape(t["DES"])}</div><div class="meta">ID {_html_escape(str(t["ID"]))}</div></div><div class="actions"><a class="btn" href="/thermostats/{_html_escape(str(t["ID"]))}">Apri</a></div></div>'
              for t in thermostats
            ]) or '<div class="row"><div class="name">Nessun termostato</div></div>'}
          </div>
          <div style="margin-top:10px;">
            <a class="btn" href="/thermostats">Lista completa</a>
          </div>
        </div>

        <div class="card">
          <h3>Programmatori orari</h3>
          <div class="muted">Gestisci programmi e timer.</div>
          <div style="margin-top:10px;">
            <a class="btn" href="/security/timers">Apri programmatori</a>
          </div>
        </div>

        <div class="card">
          <h3>Registro eventi</h3>
          <div class="muted">Log completo degli eventi.</div>
          <div style="margin-top:10px;">
            <a class="btn" href="/logs">Apri registro</a>
          </div>
        </div>

        <div class="card">
          <h3>Gestione utenti</h3>
          <div class="muted">Abilita o disabilita gli utenti della centrale.</div>
          <div style="margin-top:10px;">
            <a class="btn" href="/security/users">Apri gestione</a>
          </div>
        </div>

        <div class="card" id="reset">
          <h3>Reset rapidi</h3>
          <div class="muted">Azioni di manutenzione immediata.</div>
          <div class="list">
            <div class="row">
              <div>
                <div class="name">Reset cicli/memorie</div>
                <div class="meta">Azzera cicli e memorie</div>
              </div>
              <div class="actions">
                <button class="btn" data-reset="clear_cycles_or_memories">Reset</button>
              </div>
            </div>
            <div class="row">
              <div>
                <div class="name">Reset comunicazioni</div>
                <div class="meta">Riavvia comunicazioni bus</div>
              </div>
              <div class="actions">
                <button class="btn" data-reset="clear_communications">Reset</button>
              </div>
            </div>
            <div class="row">
              <div>
                <div class="name">Reset guasti</div>
                <div class="meta">Pulisce guasti memorizzati</div>
              </div>
              <div class="actions">
                <button class="btn" data-reset="clear_faults_memory">Reset</button>
              </div>
            </div>
          </div>
        </div>

        <div class="card" id="gsm">
          <h3>GSM</h3>
          <div class="muted">Parametri principali e stato rete.</div>
          <div class="list" id="gsmList">
            <div class="row"><div class="name">Nessun dato GSM</div></div>
          </div>
        </div>
      </div>
    </div>

    <script>
      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : txt);
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares (sessione temporanea):');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}
      async function sendCmd(type, id, action) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        const token = getPinSessionToken();
        if (token) payload.token = token;
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const text = await res.text();
        let data = null;
        try {{ data = JSON.parse(text); }} catch (_e) {{}}
        const errRaw = (data && data.error !== undefined) ? String(data.error) : '';
        const err = errRaw.trim().toLowerCase();
        const needsPin = (
          err === 'pin_session_required' ||
          err === 'invalid_token' ||
          err === 'login_failed' ||
          (data && data.ok === false && err === '')
        );
        if (needsPin) {{
          try {{ localStorage.removeItem(PIN_TOKEN_KEY); localStorage.removeItem(PIN_EXP_KEY); }} catch (_e) {{}}
          try {{
            await ensurePinSession();
          }} catch (e) {{
            const emsg = (e && e.message) ? e.message : String(e || 'PIN non valido');
            return {{ ok: false, text: emsg, data: {{ ok: false, error: emsg }} }};
          }}
          return await sendCmd(type, id, action);
        }}
        return {{ ok: res.ok, text, data }};
      }}

      function escapeHtml(s) {{
        return String(s)
          .replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;')
          .replaceAll('>', '&gt;')
          .replaceAll('\"', '&quot;')
          .replaceAll(\"'\", '&#39;');
      }}

      function bindScenarioButtons() {{
        for (const btn of document.querySelectorAll('button[data-sid]')) {{
          if (btn.dataset.bound) continue;
          btn.dataset.bound = '1';
          btn.addEventListener('click', async (ev) => {{
            const sid = Number(ev.currentTarget.getAttribute('data-sid'));
            ev.currentTarget.disabled = true;
            const res = await sendCmd('scenarios', sid, 'execute');
            ev.currentTarget.disabled = false;
            if (!res.ok) alert('Errore: ' + res.text);
          }});
        }}
      }}

      for (const btn of document.querySelectorAll('button[data-reset]')) {{
        btn.addEventListener('click', async (ev) => {{
          const action = String(ev.currentTarget.getAttribute('data-reset') || '');
          if (!action) return;
          const ok = confirm('Confermi il reset?');
          if (!ok) return;
          ev.currentTarget.disabled = true;
          const res = await sendCmd('system', 1, action);
          ev.currentTarget.disabled = false;
          if (!res.ok) alert('Errore: ' + res.text);
        }});
      }}

      function renderGsm(entities) {{
        const list = document.getElementById('gsmList');
        if (!list) return;
        const ents = Array.isArray(entities) ? entities : [];
        const gsm = ents.find(e => String(e.type || '').toLowerCase() === 'gsm');
        if (!gsm || !gsm.realtime) {{
          list.innerHTML = '<div class="row"><div class="name">Nessun dato GSM</div></div>';
          return;
        }}
        const rt = gsm.realtime || {{}};
        const items = [
          ['Operatore', rt.CARRIER],
          ['Segnale', rt.SIGNAL_PCT ? (String(rt.SIGNAL_PCT) + '%') : rt.SIGNAL],
          ['SIM', rt.SIM],
          ['Stato', rt.STA],
          ['IMEI', rt.IMEI],
          ['Credito', rt.CRE],
        ].filter(x => x[1] !== undefined && x[1] !== null && String(x[1]) !== '');
        list.innerHTML = items.map(i => `
          <div class="row">
            <div>
              <div class="name">${{escapeHtml(i[0])}}</div>
              <div class="meta">${{escapeHtml(String(i[1]))}}</div>
            </div>
          </div>
        `).join('') || '<div class="row"><div class="name">Nessun dato GSM</div></div>';
      }}

      async function loadGsm() {{
        try {{
          const res = await fetch('/api/entities', {{ cache: 'no-store' }});
          const snap = await res.json();
          renderGsm(snap.entities || []);
        }} catch (_e) {{}}
      }}

      function renderScenarios(entities, uiTags) {{
        const list = document.getElementById('scenarioList');
        if (!list) return;
        const tagMap = (uiTags && uiTags.scenarios && typeof uiTags.scenarios === 'object') ? uiTags.scenarios : {{}};
        const items = [];
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() !== 'scenarios') continue;
          const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
          const sid = Number(e.id);
          if (!Number.isFinite(sid)) continue;
          const cat = String(st.CAT || '').trim().toUpperCase();
          if (cat === 'ARM' || cat === 'DISARM' || cat === 'PARTIAL') continue;
          const tagEntry = tagMap[String(sid)];
          if (tagEntry && tagEntry.visible === false) continue;
          const name = st.DES || e.name || ('Scenario ' + String(sid));
          items.push({{ ID: sid, DES: String(name), CAT: cat || 'SMARTHOME' }});
        }}
        items.sort((a, b) => {{
          const an = a.DES.toLowerCase();
          const bn = b.DES.toLowerCase();
          if (an < bn) return -1;
          if (an > bn) return 1;
          return a.ID - b.ID;
        }});
        if (!items.length) {{
          list.innerHTML = '<div class="row"><div class="name">Nessuno scenario</div></div>';
          return;
        }}
        list.innerHTML = items.map(s => `
          <div class="row">
            <div>
              <div class="name">${{escapeHtml(s.DES)}}</div>
              <div class="meta">ID ${{escapeHtml(String(s.ID))}} · ${{escapeHtml(String(s.CAT))}}</div>
            </div>
            <div class="actions">
              <button class="btn" data-sid="${{escapeHtml(String(s.ID))}}">Esegui</button>
            </div>
          </div>
        `).join('');
        bindScenarioButtons();
      }}

      function renderThermostats(entities) {{
        const list = document.getElementById('thermostatList');
        if (!list) return;
        const items = [];
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() !== 'thermostats') continue;
          const tid = String(e.id || '');
          const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
          const name = e.name || st.DES || ('Termostato ' + tid);
          items.push({{ ID: tid, DES: String(name) }});
        }}
        items.sort((a, b) => a.DES.toLowerCase().localeCompare(b.DES.toLowerCase()));
        if (!items.length) {{
          list.innerHTML = '<div class="row"><div class="name">Nessun termostato</div></div>';
          return;
        }}
        list.innerHTML = items.map(t => `
          <div class="row">
            <div>
              <div class="name">${{escapeHtml(t.DES)}}</div>
              <div class="meta">ID ${{escapeHtml(String(t.ID))}}</div>
            </div>
            <div class="actions">
              <a class="btn" href="/thermostats/${{escapeHtml(String(t.ID))}}">Apri</a>
            </div>
          </div>
        `).join('');
      }}

      async function fetchSnap() {{
        try {{
          const [snapRes, tagsRes] = await Promise.all([
            fetch('/api/entities', {{ cache: 'no-store' }}),
            fetch('/api/ui_tags', {{ cache: 'no-store' }}),
          ]);
          const snap = await snapRes.json();
          let tags = {{}};
          try {{ tags = await tagsRes.json(); }} catch (_e) {{ tags = {{}}; }}
          const entities = snap.entities || [];
          renderScenarios(entities, tags);
          renderThermostats(entities);
          renderGsm(entities);
        }} catch (_e) {{}}
      }}

      let sse = null;
      let pollTimer = null;
      function startPolling() {{
        if (pollTimer) return;
        pollTimer = setInterval(fetchSnap, 3000);
      }}
      function startSSE() {{
        try {{
          sse = new EventSource('/api/stream');
        }} catch (_e) {{
          sse = null;
          return false;
        }}
        sse.onmessage = (ev) => {{
          try {{
            const msg = JSON.parse(ev.data || '{{}}');
            if (msg && (msg.type === 'update' || msg.entities)) {{
              fetchSnap();
            }}
          }} catch (_e) {{}}
        }};
        sse.onerror = () => {{
          try {{ if (sse) sse.close(); }} catch (_e) {{}}
          sse = null;
          startPolling();
        }};
        return true;
      }}

      bindScenarioButtons();
      fetchSnap();
      if (!startSSE()) startPolling();
      refreshTagSelectOptions();
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_scenarios(snapshot):
    # Smarthome scenarios only (exclude security categories). Rendering happens client-side from /api/entities.
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Scenari</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px 16px 48px; }}
      .controls {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom: 12px; }}
      .leftCtl {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
      .chip {{ display:inline-flex; align-items:center; gap:8px; padding: 8px 12px; border:1px solid var(--border); border-radius: 999px; background: rgba(0,0,0,0.25); }}
      .btn {{ background: rgba(0,0,0,0.10); border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.85); border-radius: 999px; padding: 8px 12px; cursor:pointer; font-size: 14px; }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      input[type="checkbox"] {{ width: 18px; height: 18px; }}
      input[type="text"] {{ width: 260px; max-width: 60vw; box-sizing:border-box; padding: 8px 10px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.28); color: rgba(255,255,255,0.92); outline:none; }}
      .list {{ border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 14px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .name {{ font-size: 18px; }}
      .sub {{ margin-top: 4px; color: rgba(255,255,255,0.65); font-size: 14px; }}
      .actions {{ display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap: wrap; }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}
      @media (max-width: 720px) {{
        .controls {{ flex-direction: column; align-items: stretch; }}
        input[type="text"] {{ width: 100%; max-width: 100%; }}
      }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab active" href="/security/scenarios">Scenari</a>
      <a class="tab" href="/security/sensors">Sensori</a>
    </div>
    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="controls">
        <div class="leftCtl">
          <span class="chip">Solo smarthome</span>
          <input id="q" type="text" placeholder="Cerca scenario..." />
          <label class="chip" style="cursor:pointer;"><input id="onlyVisible" type="checkbox" checked/> solo visibili</label>
        </div>
        <div class="leftCtl">
          <span class="chip" id="countChip">0</span>
        </div>
      </div>
      <div class="list" id="list">
        <div class="row"><div class="name">Caricamento…</div></div>
      </div>
    </div>
    <div class="toast" id="toast"></div>

    <script>
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}
      const toast = document.getElementById('toast');
      let toastTimer = null;
      function setToast(msg, ms=3500) {{
        if (!toast) return;
        toast.textContent = String(msg || '');
        toast.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{ toast.style.display = 'none'; }}, Number(ms || 3500));
      }}
      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{ return null; }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch(apiUrl('/api/cmd'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : txt);
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares:');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}
      async function sendCmd(type, id, action) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        const token = getPinSessionToken();
        if (token) payload.token = token;
        const res = await fetch(apiUrl('/api/cmd'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
        const text = await res.text();
        let data = null;
        try {{ data = JSON.parse(text); }} catch (_e) {{}}
        if (data && data.ok === false && (data.error === 'pin_session_required' || data.error === 'invalid_token')) {{
          try {{ localStorage.removeItem(PIN_TOKEN_KEY); localStorage.removeItem(PIN_EXP_KEY); }} catch (_e) {{}}
          await ensurePinSession();
          return await sendCmd(type, id, action);
        }}
        return {{ ok: res.ok, text, data }};
      }}
      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}
      function isSecurityCat(cat) {{
        const c = String(cat || '').toUpperCase();
        return (c === 'ARM' || c === 'DISARM' || c === 'PARTIAL');
      }}
      async function fetchSnap() {{
        const res = await fetch(apiUrl('/api/entities'), {{ cache: 'no-store' }});
        return await res.json();
      }}
      async function fetchUiTags() {{
        const res = await fetch(apiUrl('/api/ui_tags'), {{ cache: 'no-store' }});
        if (!res.ok) return {{}};
        return await res.json();
      }}
      function buildSmarthomeScenarios(entities, uiTags, onlyVisible, q) {{
        const list = [];
        const tagMap = (uiTags && uiTags.scenarios && typeof uiTags.scenarios === 'object') ? uiTags.scenarios : {{}};
        const needle = String(q || '').toLowerCase();
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() !== 'scenarios') continue;
          const sid = Number(e.id);
          if (!Number.isFinite(sid)) continue;
          const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
          const cat = String(st.CAT || '').trim().toUpperCase();
          if (isSecurityCat(cat)) continue;
          const entry = tagMap[String(sid)];
          if (onlyVisible && entry && entry.visible === false) continue;
          const name = String(st.DES || e.name || ('Scenario ' + String(sid)));
          const hay = (name + ' ' + cat + ' ' + String(sid)).toLowerCase();
          if (needle && !hay.includes(needle)) continue;
          list.push({{ ID: sid, DES: name, CAT: cat || 'SMARTHOME' }});
        }}
        list.sort((a,b) => (String(a.DES||'').localeCompare(String(b.DES||''), 'it', {{sensitivity:'base'}}) || (a.ID - b.ID)));
        return list;
      }}
      function renderList(items) {{
        const listEl = document.getElementById('list');
        const countEl = document.getElementById('countChip');
        if (countEl) countEl.textContent = String(items.length) + ' scenari';
        if (!listEl) return;
        if (!items.length) {{
          listEl.innerHTML = '<div class="row"><div class="name">Nessuno scenario</div></div>';
          return;
        }}
        listEl.innerHTML = items.map(s => `
          <div class="row">
            <div>
              <div class="name">${{escapeHtml(s.DES)}}</div>
              <div class="sub">ID ${{escapeHtml(String(s.ID))}} · ${{escapeHtml(String(s.CAT||''))}}</div>
            </div>
            <div class="actions">
              <button class="btn" data-sid="${{escapeHtml(String(s.ID))}}" type="button">Esegui</button>
            </div>
          </div>
        `).join('');
        for (const b of listEl.querySelectorAll('button[data-sid]')) {{
          b.addEventListener('click', async (ev) => {{
            const sid = Number(ev.currentTarget.getAttribute('data-sid'));
            setToast('Eseguo scenario ' + sid + '...');
            const res = await sendCmd('scenarios', sid, 'execute');
            const ok = !!(res && (res.ok === true) && (!res.data || res.data.ok !== false));
            setToast(ok ? 'OK' : ('Errore: ' + (res && res.data && res.data.error ? res.data.error : res.text)));
          }});
        }}
      }}
      let lastEntities = [];
      let lastUiTags = {{}};
      async function refresh() {{
        try {{
          const [snap, tags] = await Promise.all([fetchSnap(), fetchUiTags()]);
          lastEntities = Array.isArray(snap.entities) ? snap.entities : [];
          lastUiTags = (tags && typeof tags === 'object') ? tags : {{}};
          applyFilter();
        }} catch (e) {{
          setToast('Errore refresh: ' + String(e && e.message ? e.message : e));
        }}
      }}
      function applyFilter() {{
        const q = document.getElementById('q')?.value || '';
        const onlyVisible = !!document.getElementById('onlyVisible')?.checked;
        const items = buildSmarthomeScenarios(lastEntities, lastUiTags, onlyVisible, q);
        renderList(items);
      }}
      document.getElementById('q').addEventListener('input', () => applyFilter());
      document.getElementById('onlyVisible').addEventListener('change', () => applyFilter());
      // Live updates (SSE), fallback polling
      let sse = null;
      function startSSE() {{
        try {{ sse = new EventSource(apiUrl('/api/stream')); }} catch (_e) {{ sse = null; return false; }}
        sse.onmessage = () => refresh();
        sse.onerror = () => {{ try {{ if (sse) sse.close(); }} catch (_e) {{}}; sse = null; }};
        return true;
      }}
      refresh();
      startSSE();
      setInterval(refresh, 15000);
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_reset(snapshot):
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Reset rapidi</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 900px; margin: 0 auto; padding: 18px 16px 48px; }}
      .title {{ font-size: 20px; margin: 6px 0 14px; color: rgba(255,255,255,0.92); }}
      .list {{ border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 14px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .name {{ font-size: 18px; }}
      .sub {{ margin-top: 4px; color: rgba(255,255,255,0.65); font-size: 14px; }}
      .btn {{ background: rgba(0,0,0,0.10); border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.85); border-radius: 999px; padding: 8px 12px; cursor:pointer; font-size: 14px; }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security/functions" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/scenarios">Scenari</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/functions">Funzioni</a>
    </div>
    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="title">Reset rapidi</div>
      <div class="list">
        <div class="row">
          <div>
            <div class="name">Reset cicli/memorie</div>
            <div class="sub">Azzera cicli e memorie</div>
          </div>
          <div><button class="btn" data-reset="clear_cycles_or_memories" type="button">Reset</button></div>
        </div>
        <div class="row">
          <div>
            <div class="name">Reset comunicazioni</div>
            <div class="sub">Riavvia comunicazioni bus</div>
          </div>
          <div><button class="btn" data-reset="clear_communications" type="button">Reset</button></div>
        </div>
        <div class="row">
          <div>
            <div class="name">Reset guasti</div>
            <div class="sub">Pulisce guasti memorizzati</div>
          </div>
          <div><button class="btn" data-reset="clear_faults_memory" type="button">Reset</button></div>
        </div>
      </div>
    </div>
    <div class="toast" id="toast"></div>
    <script>
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}
      const toast = document.getElementById('toast');
      let toastTimer = null;
      function setToast(msg, ms=3500) {{
        if (!toast) return;
        toast.textContent = String(msg || '');
        toast.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{ toast.style.display = 'none'; }}, Number(ms || 3500));
      }}
      async function sendCmd(type, id, action) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        const res = await fetch(apiUrl('/api/cmd'), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const text = await res.text();
        let data = null;
        try {{ data = JSON.parse(text); }} catch (_e) {{}}
        return {{ ok: res.ok, text, data }};
      }}
      for (const btn of document.querySelectorAll('button[data-reset]')) {{
        btn.addEventListener('click', async (ev) => {{
          const action = String(ev.currentTarget.getAttribute('data-reset') || '');
          if (!action) return;
          if (!confirm('Confermi il reset?')) return;
          ev.currentTarget.disabled = true;
          setToast('Invio comando...');
          const res = await sendCmd('system', 1, action);
          ev.currentTarget.disabled = false;
          const ok = !!(res && (res.ok === true) && (!res.data || res.data.ok !== false));
          setToast(ok ? 'OK' : ('Errore: ' + (res && res.data && res.data.error ? res.data.error : res.text)));
        }});
      }}
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_info(snapshot):
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Info</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 900px; margin: 0 auto; padding: 18px 16px 48px; }}
      .title {{ font-size: 20px; margin: 6px 0 14px; color: rgba(255,255,255,0.92); }}
      .card {{ margin-top: 14px; border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .cardHead {{ padding: 12px 14px; background: rgba(255,255,255,0.03); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .cardHead .h {{ font-size: 16px; }}
      .cardHead .m {{ margin-top: 4px; font-size: 12px; color: rgba(255,255,255,0.65); }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 12px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .k {{ font-size: 15px; color: rgba(255,255,255,0.85); }}
      .v {{ font-size: 15px; color: rgba(255,255,255,0.92); }}
      .muted {{ color: rgba(255,255,255,0.65); font-size: 13px; padding: 12px 14px; }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security/functions" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/scenarios">Scenari</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/functions">Funzioni</a>
    </div>
    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="title">Info</div>

      <div class="card" id="gsmCard">
        <div class="cardHead">
          <div class="h">GSM</div>
          <div class="m">Operatore, segnale, credito</div>
        </div>
        <div id="gsmBody" class="muted">Caricamento...</div>
      </div>

      <div class="card" id="tempCard">
        <div class="cardHead">
          <div class="h">Temperature</div>
          <div class="m">IN / OUT</div>
        </div>
        <div id="tempBody" class="muted">Caricamento...</div>
      </div>

      <div class="card" id="verCard">
        <div class="cardHead">
          <div class="h">Versione</div>
          <div class="m">FW / WS / VM</div>
        </div>
        <div id="verBody" class="muted">Caricamento...</div>
      </div>
    </div>

    <script>
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}
      function esc(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}
      function renderKv(el, items, emptyText) {{
        if (!el) return;
        const rows = (items || []).filter(x => x && x[0] && x[1] !== undefined && x[1] !== null && String(x[1]).trim() !== '');
        if (!rows.length) {{
          el.className = 'muted';
          el.innerHTML = esc(emptyText || 'Nessun dato');
          return;
        }}
        el.className = '';
        el.innerHTML = rows.map(i => `
          <div class="row"><div class="k">${{esc(i[0])}}</div><div class="v">${{esc(String(i[1]))}}</div></div>
        `).join('');
      }}
      function isSecurityCat(cat) {{
        const c = String(cat||'').toUpperCase();
        return (c === 'ARM' || c === 'DISARM' || c === 'PARTIAL');
      }}
      function pickSystem(entities) {{
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() === 'systems') return e;
        }}
        return null;
      }}
      function pickGsm(entities) {{
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() === 'gsm') return e;
        }}
        return null;
      }}
      async function refresh() {{
        try {{
          const res = await fetch(apiUrl('/api/entities'), {{ cache: 'no-store' }});
          if (!res.ok) return;
          const snap = await res.json();
          const entities = Array.isArray(snap.entities) ? snap.entities : [];

          const gsm = pickGsm(entities);
          const gsmRt = (gsm && gsm.realtime && typeof gsm.realtime === 'object') ? gsm.realtime : null;
          const gsmItems = gsmRt ? [
            ['Operatore', gsmRt.CARRIER],
            ['Segnale', (gsmRt.SIGNAL_PCT !== undefined && gsmRt.SIGNAL_PCT !== null && String(gsmRt.SIGNAL_PCT) !== '') ? (String(gsmRt.SIGNAL_PCT) + '%') : gsmRt.SIGNAL],
            ['SIM', gsmRt.SIM],
            ['Stato', gsmRt.STA],
            ['IMEI', gsmRt.IMEI],
            ['Credito', gsmRt.CRE],
          ] : [];
          renderKv(document.getElementById('gsmBody'), gsmItems, 'Nessun dato GSM');

          const sys = pickSystem(entities);
          const sysRt = (sys && sys.realtime && typeof sys.realtime === 'object') ? sys.realtime : null;
          const sysSt = (sys && sys.static && typeof sys.static === 'object') ? sys.static : null;
          const tempObj = (sysRt && sysRt.TEMP && typeof sysRt.TEMP === 'object') ? sysRt.TEMP : ((sysSt && sysSt.TEMP && typeof sysSt.TEMP === 'object') ? sysSt.TEMP : null);
          const tempItems = tempObj ? [
            ['IN', tempObj.IN],
            ['OUT', tempObj.OUT],
          ] : [];
          renderKv(document.getElementById('tempBody'), tempItems, 'Nessuna temperatura');

          const verObj = (sysRt && sysRt.VER_LITE && typeof sysRt.VER_LITE === 'object') ? sysRt.VER_LITE : ((sysSt && sysSt.VER_LITE && typeof sysSt.VER_LITE === 'object') ? sysSt.VER_LITE : null);
          const verItems = verObj ? [
            ['FW', verObj.FW],
            ['WS_REQ', verObj.WS_REQ],
            ['WS', verObj.WS],
            ['VM_REQ', verObj.VM_REQ],
            ['VM', verObj.VM],
          ] : [];
          renderKv(document.getElementById('verBody'), verItems, 'Nessuna versione');
        }} catch (_e) {{}}
      }}
      // Live updates (SSE), fallback polling
      let sse = null;
      function startSSE() {{
        try {{ sse = new EventSource(apiUrl('/api/stream')); }} catch (_e) {{ sse = null; return false; }}
        sse.onmessage = () => refresh();
        sse.onerror = () => {{ try {{ if (sse) sse.close(); }} catch (_e) {{}}; sse = null; }};
        return true;
      }}
      refresh();
      startSSE();
      setInterval(refresh, 15000);
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_favorites(snapshot):
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Preferiti</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --card: rgba(255,255,255,0.04);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
        --ok: #1ed760;
        --warn: #ffb020;
        --bad: #ff4d4d;
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px 16px 48px; }}
      .section {{ margin-top: 18px; }}
      .sectitle {{ display:flex; align-items:center; justify-content:space-between; color: rgba(255,255,255,0.82); font-size: 18px; margin-bottom: 8px; }}
      .list {{ border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding: 14px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .name {{ font-size: 17px; }}
      .meta {{ margin-top: 4px; color: rgba(255,255,255,0.65); font-size: 13px; }}
      .actions {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
      .btn {{ background: rgba(0,0,0,0.10); border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.9); border-radius: 10px; padding: 8px 10px; cursor:pointer; font-size: 13px; }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      .pill {{ padding: 6px 10px; border-radius: 999px; font-size: 12px; color: #fff; background: rgba(255,255,255,0.1); border:1px solid rgba(255,255,255,0.14); }}
      .pill.ok {{ border-color: rgba(30,215,96,0.35); color: #1ed760; }}
      .pill.bad {{ border-color: rgba(255,77,77,0.35); color: #ff6b6b; }}
      .pill.warn {{ border-color: rgba(255,176,32,0.35); color: #ffb020; }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/scenarios">Scenari</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/favorites">Preferiti</a>
    </div>
    <div class="wrap">
      <div class="section">
        <div class="sectitle"><span>Preferiti</span><button class="btn" id="refreshBtn" type="button">Aggiorna</button></div>
        <div id="content"></div>
      </div>
    </div>
    <div class="toast" id="toast"></div>
    <script>
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}
      const toastEl = document.getElementById('toast');
      let toastTimer = null;
      function setToast(msg, ms=2400) {{
        if (!toastEl) return;
        toastEl.textContent = String(msg || '');
        toastEl.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{ toastEl.style.display = 'none'; }}, Number(ms || 2400));
      }}

      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch(apiUrl('/api/cmd'), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : txt);
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares (sessione temporanea):');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}
      async function sendCmd(type, id, action, value=null, _retry=false) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const token = getPinSessionToken();
        if (token) payload.token = token;
        const res = await fetch(apiUrl('/api/cmd'), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        const ok = res.ok && (!data || data.ok !== false);
        if (!ok && !_retry) {{
          const errRaw = (data && data.error !== undefined) ? String(data.error) : '';
          const err = errRaw.trim().toLowerCase();
          const needsPin = (
            err === 'pin_session_required' ||
            err === 'invalid_token' ||
            err === 'login_failed' ||
            (data && data.ok === false && err === '')
          );
          if (needsPin) {{
            try {{ localStorage.removeItem(PIN_TOKEN_KEY); localStorage.removeItem(PIN_EXP_KEY); }} catch (_e) {{}}
            try {{
              await ensurePinSession();
            }} catch (e) {{
              const emsg = (e && e.message) ? e.message : String(e || 'PIN non valido');
              return {{ ok:false, data: {{ ok:false, error: emsg }}, text: emsg }};
            }}
            return await sendCmd(type, id, action, value, true);
          }}
        }}
        return {{ ok, data, text: txt }};
      }}

      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}
      function zoneStatus(rt) {{
        if (!rt || typeof rt !== 'object') return {{ badges:[{{level:'ok', text:'Sconosciuto'}}], flags:{{}} }};
        const norm = (v) => String(v ?? '').trim().toUpperCase();
        const sta = norm(rt.STA);
        const t = norm(rt.T);
        const vas = norm(rt.VAS);
        const byp = norm(rt.BYP);

        const alarm = (sta === 'A');
        const excluded = !!byp && byp !== 'NO' && byp !== '0' && byp !== 'OK' && (byp.startsWith('MAN') || byp.startsWith('AUTO'));
        const tamper = !!t && t !== '0' && t !== 'N' && t !== 'OK';
        const masked = !!vas && vas !== '0' && vas !== 'F' && vas !== 'N' && vas !== 'OK';

        const badges = [];
        if (alarm) badges.push({{level:'bad', text:'Allarme'}});
        if (excluded) badges.push({{level:'warn', text:'Esclusa'}});
        if (masked) badges.push({{level:'info', text:'Mascherata'}});
        if (tamper) badges.push({{level:'warn', text:'Sabotaggio'}});
        if (!badges.length) badges.push({{level:'ok', text:'Normale'}});
        return {{ badges, flags: {{ alarm, excluded, masked, tamper }} }};
      }}
      function badge(level, text) {{
        const cls = (level === 'bad') ? 'pill bad' : (level === 'warn' ? 'pill warn' : 'pill ok');
        return '<span class=\"' + cls + '\">' + escapeHtml(text || '') + '</span>';
      }}

      let entities = [];
      let favs = {{ outputs: {{}}, scenarios: {{}}, zones: {{}}, partitions: {{}} }};

      async function fetchFavs() {{
        const res = await fetch(apiUrl('/api/ui_favorites'), {{ cache: 'no-store' }});
        const data = await res.json();
        if (data && typeof data === 'object') favs = data;
      }}
      async function fetchSnap() {{
        const res = await fetch(apiUrl('/api/entities'), {{ cache: 'no-store' }});
        const snap = await res.json();
        entities = Array.isArray(snap.entities) ? snap.entities : [];
      }}

      function entBy(type, id) {{
        id = String(id);
        return (entities || []).find(e => String(e.type || '') === String(type) && String(e.id || '') === id);
      }}

      function render() {{
        const content = document.getElementById('content');
        if (!content) return;
        const sections = [];
        const order = ['outputs','partitions','scenarios','zones'];
        for (const t of order) {{
          const bucket = (favs && favs[t] && typeof favs[t] === 'object') ? favs[t] : {{}};
          const ids = Object.keys(bucket);
          if (!ids.length) continue;
          const rows = [];
          ids.sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
          for (const id of ids) {{
            const e = entBy(t, id);
            if (!e) continue;
            const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
            const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
            const name = e.name || st.DES || ('ID ' + id);
            let meta = '';
            let status = '';
            let actions = '';
            if (t === 'outputs') {{
              const sta = String(rt.STA || '').toUpperCase();
              meta = 'Cat: ' + (st.CAT || st.TYP || '-');
              if (sta) status = badge(sta === 'ON' ? 'ok' : 'warn', 'STA ' + sta);
              actions = '<button class=\"btn\" data-act=\"on\" data-id=\"' + id + '\" data-type=\"outputs\">ON</button>' +
                        '<button class=\"btn\" data-act=\"off\" data-id=\"' + id + '\" data-type=\"outputs\">OFF</button>' +
                        '<button class=\"btn\" data-act=\"toggle\" data-id=\"' + id + '\" data-type=\"outputs\">TOGGLE</button>';
            }} else if (t === 'partitions') {{
              const arm = rt.ARM || '';
              status = badge(String(arm).trim().toUpperCase() === 'D' ? 'ok' : 'warn', 'ARM ' + String(arm || '-'));
              actions = '<button class=\"btn\" data-act=\"arm\" data-id=\"' + id + '\" data-type=\"partitions\">Inserisci</button>' +
                        '<button class=\"btn\" data-act=\"arm_partial\" data-id=\"' + id + '\" data-type=\"partitions\">Parziale</button>' +
                        '<button class=\"btn\" data-act=\"disarm\" data-id=\"' + id + '\" data-type=\"partitions\">Disinserisci</button>';
            }} else if (t === 'scenarios') {{
              meta = 'Cat: ' + (st.CAT || '-');
              actions = '<button class=\"btn\" data-act=\"execute\" data-id=\"' + id + '\" data-type=\"scenarios\">Esegui</button>';
            }} else if (t === 'zones') {{
              const zs = zoneStatus(rt);
              if (zs && zs.badges && zs.badges.length) status = badge(zs.badges[0].level, zs.badges[0].text);
              actions = '<button class=\"btn\" data-act=\"bypass_on\" data-id=\"' + id + '\" data-type=\"zones\">Escludi</button>' +
                        '<button class=\"btn\" data-act=\"bypass_off\" data-id=\"' + id + '\" data-type=\"zones\">Includi</button>' +
                        '<button class=\"btn\" data-act=\"bypass_toggle\" data-id=\"' + id + '\" data-type=\"zones\">Toggle</button>';
            }}
            rows.push(
              '<div class=\"row\">' +
                '<div>' +
                  '<div class=\"name\">' + escapeHtml(name) + '</div>' +
                  '<div class=\"meta\">ID ' + escapeHtml(id) + (meta ? ' · ' + escapeHtml(meta) : '') + '</div>' +
                  (status ? ('<div style=\"margin-top:6px;\">' + status + '</div>') : '') +
                '</div>' +
                '<div class=\"actions\">' +
                  actions +
                  '<button class=\"btn\" data-act=\"remove\" data-id=\"' + id + '\" data-type=\"' + t + '\">Rimuovi</button>' +
                '</div>' +
              '</div>'
            );
          }}
          if (rows.length) {{
            sections.push(
              '<div class=\"section\">' +
                '<div class=\"sectitle\">' + escapeHtml(t.charAt(0).toUpperCase() + t.slice(1)) + '</div>' +
                '<div class=\"list\">' + rows.join('') + '</div>' +
              '</div>'
            );
          }}
        }}
        content.innerHTML = sections.join('') || '<div class=\"muted\">Nessun preferito</div>';
        for (const btn of content.querySelectorAll('.btn[data-act]')) {{
          btn.addEventListener('click', async (ev) => {{
            const type = ev.currentTarget.getAttribute('data-type');
            const id = Number(ev.currentTarget.getAttribute('data-id'));
            const act = ev.currentTarget.getAttribute('data-act');
            try {{
              if (act === 'remove') {{
                await toggleFav(type, id, false);
                setToast('Rimosso');
                await refresh();
                return;
              }}
              setToast('Invio comando...');
              const res = await sendCmd(type, id, act);
              const ok = !!(res && res.ok);
              setToast(ok ? 'OK' : ('Errore: ' + (res && res.data && res.data.error ? res.data.error : res.text)));
              if (ok) setTimeout(refresh, 400);
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }}
          }});
        }}
      }}

      async function toggleFav(type, id, fav) {{
        await fetch(apiUrl('/api/ui_favorites'), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ type, id, fav }})
        }});
      }}

      async function refresh() {{
        await fetchFavs();
        await fetchSnap();
        render();
      }}

      document.getElementById('refreshBtn').addEventListener('click', () => refresh());
      refresh();
    </script>
  </body>
</html>"""
    return html.encode("utf-8")

def render_security_users(snapshot):
    # Accounts/users (CFG_ACCOUNTS). Rendering happens client-side from /api/entities.
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Gestione Utenti</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --row: rgba(255,255,255,0.06);
        --row2: rgba(255,255,255,0.045);
        --ok: #1ed760;
        --bad: #ff4d4d;
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px 16px 48px; }}
      .controls {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom: 12px; }}
      .leftCtl {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
      .chip {{ display:inline-flex; align-items:center; gap:8px; padding: 8px 12px; border:1px solid var(--border); border-radius: 999px; background: rgba(0,0,0,0.25); }}
      .chip.ok {{ border-color: rgba(30,215,96,0.30); color: rgba(30,215,96,0.95); }}
      .chip.bad {{ border-color: rgba(255,77,77,0.30); color: rgba(255,77,77,0.95); }}
      .btn {{ background: rgba(0,0,0,0.10); border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.85); border-radius: 999px; padding: 8px 12px; cursor:pointer; font-size: 14px; }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      input[type="checkbox"] {{ width: 18px; height: 18px; }}
      input[type="text"] {{ width: 260px; max-width: 60vw; box-sizing:border-box; padding: 8px 10px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.28); color: rgba(255,255,255,0.92); outline:none; }}
      .list {{ border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; overflow:hidden; }}
      .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding: 14px 14px; background: var(--row); border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .row:nth-child(even) {{ background: var(--row2); }}
      .row:last-child {{ border-bottom: 0; }}
      .name {{ font-size: 18px; }}
      .sub {{ margin-top: 4px; color: rgba(255,255,255,0.65); font-size: 14px; }}
      .actions {{ display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap: wrap; }}
      .badge {{
        font-size: 12px;
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 999px;
        padding: 6px 10px;
        background: rgba(0,0,0,0.18);
        color: rgba(255,255,255,0.88);
      }}
      .badge.ok {{ border-color: rgba(30,215,96,0.30); color: rgba(30,215,96,0.95); }}
      .badge.bad {{ border-color: rgba(255,77,77,0.30); color: rgba(255,77,77,0.95); }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}
      @media (max-width: 720px) {{
        .controls {{ flex-direction: column; align-items: stretch; }}
        input[type="text"] {{ width: 100%; max-width: 100%; }}
      }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security/functions" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/functions">Funzioni</a>
    </div>
    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="controls">
        <div class="leftCtl">
          <span class="chip" id="countChip">0 utenti</span>
          <label class="chip"><input id="onlyDisabled" type="checkbox"/> Solo disabilitati</label>
          <input id="q" type="text" placeholder="Filtra (nome/livello/partizioni/id)..." />
        </div>
        <div class="leftCtl">
          <button class="btn" id="refreshBtn" type="button">Aggiorna</button>
        </div>
      </div>
      <div class="list" id="list"></div>
    </div>
    <div class="toast" id="toast"></div>
    <script>
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}
      const toastEl = document.getElementById('toast');
      let toastTimer = null;
      function setToast(msg, ms=2600) {{
        if (!toastEl) return;
        toastEl.textContent = String(msg || '');
        toastEl.style.display = 'block';
        if (toastTimer) {{ try {{ clearTimeout(toastTimer); }} catch (_e) {{}} }}
        toastTimer = setTimeout(() => {{ toastEl.style.display = 'none'; }}, Number(ms || 2600));
      }}

      const PIN_TOKEN_KEY = 'ksenia_pin_session_token';
      const PIN_EXP_KEY = 'ksenia_pin_session_expires_at';
      function getPinSessionToken() {{
        try {{
          const token = localStorage.getItem(PIN_TOKEN_KEY) || '';
          const expRaw = localStorage.getItem(PIN_EXP_KEY) || '';
          const exp = Number(expRaw || '0');
          if (!token) return null;
          if (!exp || Date.now() >= exp * 1000) {{
            localStorage.removeItem(PIN_TOKEN_KEY);
            localStorage.removeItem(PIN_EXP_KEY);
            return null;
          }}
          return token;
        }} catch (_e) {{
          return null;
        }}
      }}
      async function startPinSession(pin, minutes=5) {{
        const payload = {{ type: 'session', action: 'start', value: {{ pin: String(pin || ''), minutes: Number(minutes || 5) }} }};
        const res = await fetch(apiUrl('/api/cmd'), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || !data || !data.ok) throw new Error((data && data.error) ? data.error : txt);
        try {{
          localStorage.setItem(PIN_TOKEN_KEY, String(data.token || ''));
          localStorage.setItem(PIN_EXP_KEY, String(data.expires_at || ''));
        }} catch (_e) {{}}
        return String(data.token || '');
      }}
      async function ensurePinSession() {{
        const existing = getPinSessionToken();
        if (existing) return existing;
        const pin = prompt('Inserisci PIN Lares:');
        if (!pin) throw new Error('PIN non inserito');
        return await startPinSession(pin, 5);
      }}
      async function sendCmd(type, id, action) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        const token = getPinSessionToken();
        if (token) payload.token = token;
        const res = await fetch(apiUrl('/api/cmd'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
        const text = await res.text();
        let data = null;
        try {{ data = JSON.parse(text); }} catch (_e) {{}}
        if (data && data.ok === false && (data.error === 'pin_session_required' || data.error === 'invalid_token')) {{
          try {{ localStorage.removeItem(PIN_TOKEN_KEY); localStorage.removeItem(PIN_EXP_KEY); }} catch (_e) {{}}
          await ensurePinSession();
          return await sendCmd(type, id, action);
        }}
        return {{ ok: res.ok, text, data }};
      }}

      function escapeHtml(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}
      function isEnabled(st) {{
        // Prefer DACC (Disable Account) flag if present; fallback to VALIDITY.ALWAYS.
        const dacc = (st && typeof st === 'object') ? String(st.DACC || '').trim().toUpperCase() : '';
        if (dacc === 'T' || dacc === '1' || dacc === 'ON' || dacc === 'TRUE') return false;
        if (dacc === 'F' || dacc === '0' || dacc === 'OFF' || dacc === 'FALSE') return true;
        const validity = (st && typeof st === 'object') ? st.VALIDITY : null;
        const always = (validity && typeof validity === 'object') ? String(validity.ALWAYS || 'T').trim().toUpperCase() : 'T';
        return always !== 'F';
      }}
      function buildAccounts(entities, onlyDisabled, q) {{
        const list = [];
        const needle = String(q || '').toLowerCase();
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() !== 'accounts') continue;
          const id = Number(e.id);
          if (!Number.isFinite(id)) continue;
          const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
          const name = String(st.DES || e.name || ('Utente ' + String(id)));
          const lev = String(st.LEV || '').trim();
          const prt = String(st.PRT || '').trim();
          const enabled = isEnabled(st);
          if (onlyDisabled && enabled) continue;
          const hay = (name + ' ' + lev + ' ' + prt + ' ' + String(id)).toLowerCase();
          if (needle && !hay.includes(needle)) continue;
          list.push({{ ID: id, DES: name, LEV: lev, PRT: prt, enabled: enabled }});
        }}
        list.sort((a,b) => (String(a.DES||'').localeCompare(String(b.DES||''), 'it', {{sensitivity:'base'}}) || (a.ID - b.ID)));
        return list;
      }}
      function renderList(items) {{
        const listEl = document.getElementById('list');
        const countEl = document.getElementById('countChip');
        if (countEl) countEl.textContent = String(items.length) + ' utenti';
        if (!listEl) return;
        if (!items.length) {{
          listEl.innerHTML = '<div class="row"><div class="name">Nessun utente</div></div>';
          return;
        }}
        listEl.innerHTML = items.map(u => `
          <div class="row">
            <div>
              <div class="name">${{escapeHtml(u.DES)}}</div>
              <div class="sub">ID ${{escapeHtml(String(u.ID))}} Жњ LEV ${{escapeHtml(String(u.LEV||'-'))}} Жњ PRT ${{escapeHtml(String(u.PRT||'-'))}}</div>
            </div>
            <div class="actions">
              <span class="badge ${{u.enabled ? 'ok' : 'bad'}}">${{u.enabled ? 'Abilitato' : 'Disabilitato'}}</span>
              <button class="btn" data-id="${{escapeHtml(String(u.ID))}}" data-enabled="${{u.enabled ? '1' : '0'}}" type="button">${{u.enabled ? 'Disabilita' : 'Abilita'}}</button>
            </div>
          </div>
        `).join('');
        for (const b of listEl.querySelectorAll('button[data-id]')) {{
          b.addEventListener('click', async (ev) => {{
            const id = Number(ev.currentTarget.getAttribute('data-id'));
            const enabled = String(ev.currentTarget.getAttribute('data-enabled')||'0') === '1';
            const act = enabled ? 'disable' : 'enable';
            ev.currentTarget.disabled = true;
            setToast((enabled ? 'Disabilito' : 'Abilito') + ' utente ' + id + '...');
            try {{
              const res = await sendCmd('accounts', id, act);
              const ok = !!(res && (res.ok === true) && (!res.data || res.data.ok !== false));
              setToast(ok ? 'OK' : ('Errore: ' + (res && res.data && res.data.error ? res.data.error : res.text)));
              if (ok) setTimeout(refresh, 400);
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }} finally {{
              try {{ ev.currentTarget.disabled = false; }} catch (_e) {{}}
            }}
          }});
        }}
      }}

      let lastEntities = [];
      async function fetchSnap() {{
        const res = await fetch(apiUrl('/api/entities'), {{ cache: 'no-store' }});
        return await res.json();
      }}
      async function refresh() {{
        try {{
          const snap = await fetchSnap();
          lastEntities = Array.isArray(snap.entities) ? snap.entities : [];
          applyFilter();
        }} catch (e) {{
          setToast('Errore refresh: ' + String(e && e.message ? e.message : e));
        }}
      }}
      function applyFilter() {{
        const q = document.getElementById('q')?.value || '';
        const onlyDisabled = !!document.getElementById('onlyDisabled')?.checked;
        const items = buildAccounts(lastEntities, onlyDisabled, q);
        renderList(items);
      }}
      document.getElementById('q').addEventListener('input', () => applyFilter());
      document.getElementById('onlyDisabled').addEventListener('change', () => applyFilter());
      document.getElementById('refreshBtn').addEventListener('click', () => refresh());

      // Live updates (SSE), fallback polling
      let sse = null;
      let pollT = null;
      function startPolling() {{
        if (pollT) return;
        pollT = setInterval(refresh, 2500);
      }}
      function stopPolling() {{
        if (!pollT) return;
        try {{ clearInterval(pollT); }} catch (_e) {{}}
        pollT = null;
      }}
      function startSSE() {{
        if (!window.EventSource) return false;
        try {{ sse = new EventSource(apiUrl('/api/stream')); }} catch (_e) {{ sse = null; return false; }}
        sse.onmessage = () => refresh();
        sse.onerror = () => {{ try {{ if (sse) sse.close(); }} catch (_e) {{}}; sse = null; startPolling(); }};
        return true;
      }}

      refresh();
      if (!startSSE()) startPolling();
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_timers(snapshot):
    entities = snapshot.get("entities") or []
    meta = snapshot.get("meta") or {}

    scenarios = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "scenarios":
            continue
        try:
            sid = int(e.get("id"))
        except Exception:
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        name = st.get("DES") or st.get("NM") or e.get("name")
        if isinstance(name, str) and name.strip():
            scenarios[str(sid)] = name.strip()

    timers = []
    for e in entities:
        if str(e.get("type") or "").lower() != "schedulers":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        item = {**st, **rt}
        if "ID" not in item:
            item["ID"] = e.get("id")
        sce = item.get("SCE")
        try:
            if sce is not None:
                item["SCE_NAME"] = scenarios.get(str(int(str(sce)))) or item.get("SCE_NAME")
        except Exception:
            pass
        timers.append(item)

    def _id_key(x):
        try:
            return int(str(x.get("ID") or 0))
        except Exception:
            return 0

    timers.sort(key=_id_key)
    init_payload = _html_escape(
        json.dumps({"timers": timers, "scenarios": scenarios, "meta": meta}, ensure_ascii=False)
    )

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Programmatori orari</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: rgba(255,255,255,0.65);
        --border: rgba(255,255,255,0.10);
        --card: rgba(255,255,255,0.06);
        --card2: rgba(255,255,255,0.045);
        --ok: rgba(80, 255, 140, 0.18);
        --ok2: rgba(80, 255, 140, 0.35);
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .tab {{
        font-size: 18px; letter-spacing: 0.5px;
        color: rgba(255,255,255,0.75);
        text-decoration:none;
        padding: 10px 14px;
        border-radius: 12px;
      }}
      .tab.active {{ color: #fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 18px 16px 64px; }}
      .title {{ font-size: 20px; margin: 6px 0 6px; color: rgba(255,255,255,0.92); }}
      .meta {{ font-size: 12px; color: rgba(255,255,255,0.55); }}

      .toolbar {{
        display:flex;
        gap: 10px;
        align-items:center;
        flex-wrap: wrap;
        margin-top: 12px;
        margin-bottom: 14px;
      }}
      .q {{
        flex: 1;
        min-width: 220px;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.92);
        outline: none;
      }}
      .chip {{
        display:inline-flex;
        align-items:center;
        gap: 8px;
        padding: 10px 12px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.18);
        color: rgba(255,255,255,0.86);
        user-select: none;
      }}
      .chip input {{ width: 18px; height: 18px; }}

      .grid {{
        display:grid;
        grid-template-columns: 1fr;
        gap: 12px;
      }}
      @media (min-width: 860px) {{
        .grid {{ grid-template-columns: 1fr 380px; gap: 14px; }}
      }}

      .list {{
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        overflow: hidden;
        background: rgba(255,255,255,0.02);
      }}
      .row {{
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap: 10px;
        padding: 12px 14px;
        background: var(--card);
        border-bottom: 1px solid rgba(255,255,255,0.06);
        cursor: pointer;
      }}
      .row:nth-child(even) {{ background: var(--card2); }}
      .row:last-child {{ border-bottom: 0; }}
      .row.active {{
        outline: 1px solid rgba(80,255,140,0.18);
        box-shadow: 0 0 0 1px rgba(80,255,140,0.10) inset;
      }}
      .left {{
        min-width: 0;
        display:flex;
        flex-direction:column;
        gap: 4px;
      }}
      .name {{
        font-size: 16px;
        color: rgba(255,255,255,0.92);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .sub {{
        font-size: 12px;
        color: rgba(255,255,255,0.65);
        display:flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
      .pill {{
        display:inline-flex;
        align-items:center;
        gap: 8px;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.12);
        background: rgba(0,0,0,0.18);
        font-size: 12px;
        color: rgba(255,255,255,0.88);
      }}
      .pill.on {{ border-color: rgba(80,255,140,0.35); background: var(--ok); }}
      .pill.off {{ border-color: rgba(255,255,255,0.10); background: rgba(0,0,0,0.12); }}

      .detail {{
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        overflow: hidden;
        background: rgba(255,255,255,0.02);
      }}
      .detailHead {{
        padding: 12px 14px;
        background: rgba(255,255,255,0.03);
        border-bottom: 1px solid rgba(255,255,255,0.06);
        display:flex;
        align-items:flex-start;
        justify-content: space-between;
        gap: 10px;
      }}
      .detailTitle {{
        font-size: 16px;
        color: rgba(255,255,255,0.92);
      }}
      .detailMeta {{
        margin-top: 3px;
        font-size: 12px;
        color: rgba(255,255,255,0.6);
      }}
      .btn {{
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.12);
        background: rgba(0,0,0,0.18);
        color: rgba(255,255,255,0.9);
        padding: 8px 10px;
        cursor: pointer;
      }}
      .btn:hover {{ background: rgba(255,255,255,0.06); }}
      .body {{
        padding: 12px 14px;
      }}
      .section {{
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 14px;
        overflow: hidden;
        margin-bottom: 12px;
      }}
      .sectionHead {{
        padding: 10px 12px;
        background: rgba(255,255,255,0.03);
        border-bottom: 1px solid rgba(255,255,255,0.06);
        font-size: 13px;
        color: rgba(255,255,255,0.86);
      }}
      .field {{
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 12px;
        padding: 10px 12px;
        background: rgba(255,255,255,0.02);
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .field:last-child {{ border-bottom: 0; }}
      .label {{
        font-size: 12px;
        color: rgba(255,255,255,0.62);
      }}
      .value {{
        font-size: 14px;
        color: rgba(255,255,255,0.92);
      }}
      .input, select {{
        padding: 8px 10px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.92);
        outline: none;
      }}
      select {{ max-width: 100%; }}
      .days {{
        display:flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
      .day {{
        display:inline-flex;
        align-items:center;
        gap: 8px;
        padding: 8px 10px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.14);
        cursor: pointer;
        user-select: none;
      }}
      .day.on {{
        border-color: rgba(80,255,140,0.35);
        box-shadow: 0 0 0 1px rgba(80,255,140,0.10) inset;
        background: rgba(80,255,140,0.14);
      }}
      .day span {{ font-size: 12px; color: rgba(255,255,255,0.85); }}

      .toast {{
        position: fixed;
        left: 50%;
        bottom: 18px;
        transform: translateX(-50%);
        background: rgba(0,0,0,0.65);
        border: 1px solid rgba(255,255,255,0.10);
        color: rgba(255,255,255,0.92);
        padding: 10px 14px;
        border-radius: 12px;
        backdrop-filter: blur(10px);
        display:none;
        z-index: 10;
        min-width: 180px;
        text-align: center;
      }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security/functions" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/scenarios">Scenari</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/functions">Funzioni</a>
    </div>

    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="title">Programmatori orari</div>
      <div class="meta">Ultimo aggiornamento: <span id="lastUpdate">-</span> · Totale: <span id="count">0</span></div>

      <div class="toolbar">
        <input id="q" class="q" placeholder="Cerca (descrizione, scenario, orario, giorni)..." />
        <label class="chip"><input id="onlyOn" type="checkbox"/> Solo attivi</label>
      </div>

      <div class="panel" id="panelSchedule">
        <div class="list" id="list"></div>
        <div class="detail" id="detail"></div>
      </div>
    </div>

    <div class="toast" id="toast"></div>
    <script id="init" type="application/json">{init_payload}</script>
    <script>
      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}
      function esc(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}
      function setToast(msg) {{
        const el = document.getElementById('toast');
        if (!el) return;
        el.textContent = String(msg || '');
        el.style.display = 'block';
        clearTimeout(setToast._t);
        setToast._t = setTimeout(() => {{ el.style.display = 'none'; }}, 1400);
      }}

      const DAYS = [
        ['MON','Lun'], ['TUE','Mar'], ['WED','Mer'], ['THU','Gio'], ['FRI','Ven'], ['SAT','Sab'], ['SUN','Dom']
      ];

      let sse = null;
      let timersById = new Map();
      let ids = [];
      let scenarios = {{}};
      let selectedId = null;

      function isEnabled(it) {{
        return String(it && it.EN ? it.EN : '').toUpperCase() === 'T';
      }}
      function timeStr(it) {{
        const h = it && it.H !== undefined ? it.H : undefined;
        const m = it && it.M !== undefined ? it.M : undefined;
        if (h === undefined || m === undefined) return '';
        const hh = String(parseInt(h,10)).padStart(2,'0');
        const mm = String(parseInt(m,10)).padStart(2,'0');
        return hh + ':' + mm;
      }}
      function daysStr(it) {{
        const out = [];
        for (const pair of DAYS) {{
          const k = pair[0], lab = pair[1];
          if (String(it && it[k] !== undefined ? it[k] : '').toUpperCase() === 'T') out.push(lab);
        }}
        return out.join(', ');
      }}
      function scenarioName(it) {{
        const sid = (it && it.SCE !== undefined && it.SCE !== null) ? String(it.SCE) : '';
        const nm = it && it.SCE_NAME ? String(it.SCE_NAME) : '';
        if (nm) return nm;
        if (sid && scenarios && scenarios[sid]) return String(scenarios[sid]);
        return sid ? ('ID ' + sid) : '';
      }}

      function parseInit() {{
        let payload = null;
        try {{
          payload = JSON.parse(document.getElementById('init').textContent || '{{}}');
        }} catch (_e) {{
          payload = null;
        }}
        if (!payload) payload = {{}};
        scenarios = (payload.scenarios && typeof payload.scenarios === 'object') ? payload.scenarios : {{}};
        const list = Array.isArray(payload.timers) ? payload.timers : [];
        timersById = new Map();
        ids = [];
        for (const it of list) {{
          if (!it || it.ID === undefined || it.ID === null) continue;
          const id = String(it.ID);
          timersById.set(id, it);
          ids.push(id);
        }}
        ids.sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
        document.getElementById('count').textContent = String(ids.length);
        const m = payload.meta && typeof payload.meta === 'object' ? payload.meta : null;
        const ts = m && m.last_update ? Number(m.last_update) : 0;
        document.getElementById('lastUpdate').textContent = ts ? new Date(ts * 1000).toISOString().replace('T',' ').slice(0,19) : '-';
      }}

      function filteredIds() {{
        const q = String(document.getElementById('q').value || '').trim().toLowerCase();
        const onlyOn = !!document.getElementById('onlyOn').checked;
        const out = [];
        for (const id of ids) {{
          const it = timersById.get(id);
          if (!it) continue;
          if (onlyOn && !isEnabled(it)) continue;
          if (!q) {{ out.push(id); continue; }}
          const hay = (
            String(it.DES||'') + ' ' +
            String(scenarioName(it)||'') + ' ' +
            String(timeStr(it)||'') + ' ' +
            String(daysStr(it)||'')
          ).toLowerCase();
          if (hay.includes(q)) out.push(id);
        }}
        return out;
      }}

      function renderList() {{
        const list = filteredIds();
        if (selectedId === null && list.length) selectedId = list[0];
        const rows = [];
        for (const id of list) {{
          const it = timersById.get(id);
          if (!it) continue;
          const en = isEnabled(it);
          const name = String(it.DES || ('Programmatore ' + String(id)));
          const when = timeStr(it);
          const days = daysStr(it);
          const sce = scenarioName(it);
          rows.push(
            '<div class=\"row ' + (String(selectedId) === String(id) ? 'active' : '') + '\" data-id=\"' + esc(id) + '\">' +
              '<div class=\"left\">' +
                '<div class=\"name\">' + esc(name) + '</div>' +
                '<div class=\"sub\">' +
                  '<span class=\"pill ' + (en ? 'on' : 'off') + '\">' + (en ? 'ON' : 'OFF') + '</span>' +
                  '<span class=\"pill\">' + esc(when || '--:--') + '</span>' +
                  (days ? '<span class=\"pill\">' + esc(days) + '</span>' : '') +
                  (sce ? '<span class=\"pill\">' + esc(sce) + '</span>' : '') +
                '</div>' +
              '</div>' +
              '<div class=\"pill\">ID ' + esc(id) + '</div>' +
            '</div>'
          );
        }}
        document.getElementById('list').innerHTML = rows.join('') || '<div class=\"row\"><div class=\"left\"><div class=\"name\">Nessun programmatore</div><div class=\"sub\">Nessun risultato</div></div></div>';

        for (const el of document.querySelectorAll('.row[data-id]')) {{
          el.onclick = () => {{
            selectedId = el.dataset.id;
            render();
          }};
        }}
      }}

      function scenarioOptions(current) {{
        const opts = [];
        opts.push('<option value=\"0\">Nessuno</option>');
        const keys = Object.keys(scenarios || {{}}).sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
        for (const sid of keys) {{
          const name = scenarios[sid];
          const sel = String(current) === String(sid) ? ' selected' : '';
          opts.push('<option value=\"' + esc(sid) + '\"' + sel + '>' + esc(name) + '</option>');
        }}
        return opts.join('');
      }}

      function renderDetail() {{
        const id = selectedId !== null ? String(selectedId) : '';
        const it = id ? timersById.get(id) : null;
        if (!it) {{
          document.getElementById('detail').innerHTML = '<div class=\"detailHead\"><div><div class=\"detailTitle\">Dettagli</div><div class=\"detailMeta\">Seleziona un programmatore</div></div></div>';
          return;
        }}
        const en = isEnabled(it);
        const name = String(it.DES || ('Programmatore ' + String(id)));
        const when = timeStr(it);
        const sceId = (it.SCE !== undefined && it.SCE !== null) ? String(it.SCE) : '0';
        const excl = String(it.EXCL_HOLIDAYS ?? '').toUpperCase() === 'T';
        const typ = String(it.TYPE ?? 'TIME');

        const dayBtns = DAYS.map(pair => {{
          const k = pair[0], lab = pair[1];
          const on = String(it[k] ?? '').toUpperCase() === 'T';
          return '<div class=\"day ' + (on ? 'on' : '') + '\" data-day=\"' + esc(k) + '\" data-id=\"' + esc(id) + '\"><span>' + esc(lab) + '</span></div>';
        }}).join('');

        document.getElementById('detail').innerHTML =
          '<div class=\"detailHead\">' +
            '<div>' +
              '<div class=\"detailTitle\">' + esc(name) + '</div>' +
              '<div class=\"detailMeta\">ID ' + esc(id) + ' · Tipo: ' + esc(typ) + '</div>' +
            '</div>' +
            '<button class=\"btn\" id=\"btnRefresh\" type=\"button\">Aggiorna</button>' +
          '</div>' +
          '<div class=\"body\">' +
            '<div class=\"section\">' +
              '<div class=\"sectionHead\">Generali</div>' +
              '<div class=\"field\">' +
                '<div><div class=\"label\">Abilitato</div><div class=\"value\">' + (en ? 'ON' : 'OFF') + '</div></div>' +
                '<label class=\"chip\"><input id=\"enToggle\" type=\"checkbox\" ' + (en ? 'checked' : '') + '/> Attivo</label>' +
              '</div>' +
              '<div class=\"field\">' +
                '<div style=\"min-width:120px\"><div class=\"label\">Descrizione</div></div>' +
                '<input id=\"desInput\" class=\"input\" value=\"' + esc(String(it.DES||'')) + '\" style=\"flex:1\" />' +
              '</div>' +
              '<div class=\"field\">' +
                '<div style=\"min-width:120px\"><div class=\"label\">Scenario</div></div>' +
                '<select id=\"sceSel\">' + scenarioOptions(sceId) + '</select>' +
              '</div>' +
            '</div>' +
            '<div class=\"section\">' +
              '<div class=\"sectionHead\">Orario</div>' +
              '<div class=\"field\">' +
                '<div><div class=\"label\">Ora</div><div class=\"value\">' + esc(when || '--:--') + '</div></div>' +
                '<input id=\"timeInput\" class=\"input\" type=\"time\" value=\"' + esc(when || '') + '\" />' +
              '</div>' +
            '</div>' +
            '<div class=\"section\">' +
              '<div class=\"sectionHead\">Ripetizioni</div>' +
              '<div class=\"field\" style=\"align-items:flex-start\">' +
                '<div style=\"min-width:120px\"><div class=\"label\">Giorni</div></div>' +
                '<div class=\"days\" id=\"daysBox\">' + dayBtns + '</div>' +
              '</div>' +
            '</div>' +
            '<div class=\"section\">' +
              '<div class=\"sectionHead\">Festivi</div>' +
              '<div class=\"field\">' +
                '<div><div class=\"label\">Escludi festivi</div><div class=\"value\">' + (excl ? 'SI' : 'NO') + '</div></div>' +
                '<label class=\"chip\"><input id=\"holToggle\" type=\"checkbox\" ' + (excl ? 'checked' : '') + '/> Escludi</label>' +
              '</div>' +
            '</div>' +
          '</div>';

        document.getElementById('btnRefresh').onclick = () => refreshOnce();
        wireDetailControls(id);
      }}

      async function sendCmd(type, id, action, value=null) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const res = await fetch(apiUrl('/api/cmd'), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        let data = null;
        try {{ data = await res.json(); }} catch (_e) {{ data = null; }}
        if (!res.ok || !data || data.ok !== true) {{
          const err = (data && data.error) ? data.error : (res.status + '');
          throw new Error(err);
        }}
        return data;
      }}

      function wireDetailControls(id) {{
        const enToggle = document.getElementById('enToggle');
        if (enToggle) {{
          enToggle.onchange = async () => {{
            const val = enToggle.checked ? 'ON' : 'OFF';
            try {{
              await sendCmd('schedulers', id, 'set_enabled', val);
              setToast('OK');
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }}
          }};
        }}
        const desInput = document.getElementById('desInput');
        if (desInput) {{
          desInput.onchange = async () => {{
            const v = String(desInput.value || '').trim();
            if (!v) return;
            try {{
              await sendCmd('schedulers', id, 'set_description', v);
              setToast('OK');
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }}
          }};
        }}
        const sceSel = document.getElementById('sceSel');
        if (sceSel) {{
          sceSel.onchange = async () => {{
            const v = Number(sceSel.value || '0');
            try {{
              await sendCmd('schedulers', id, 'set_scenario', v);
              setToast('OK');
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }}
          }};
        }}
        const timeInput = document.getElementById('timeInput');
        if (timeInput) {{
          timeInput.onchange = async () => {{
            const v = String(timeInput.value || '').trim();
            if (!v) return;
            try {{
              await sendCmd('schedulers', id, 'set_time', v);
              setToast('OK');
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }}
          }};
        }}
        const holToggle = document.getElementById('holToggle');
        if (holToggle) {{
          holToggle.onchange = async () => {{
            const val = holToggle.checked ? 'ON' : 'OFF';
            try {{
              await sendCmd('schedulers', id, 'set_excl_holidays', val);
              setToast('OK');
            }} catch (e) {{
              setToast('Errore: ' + String(e && e.message ? e.message : e));
            }}
          }};
        }}
        const daysBox = document.getElementById('daysBox');
        if (daysBox) {{
          for (const btn of daysBox.querySelectorAll('.day[data-day]')) {{
            btn.onclick = async () => {{
              btn.classList.toggle('on');
              const patch = {{}};
              for (const x of daysBox.querySelectorAll('.day[data-day]')) {{
                patch[String(x.getAttribute('data-day'))] = x.classList.contains('on');
              }}
              try {{
                await sendCmd('schedulers', id, 'set_days', patch);
                setToast('OK');
              }} catch (e) {{
                setToast('Errore: ' + String(e && e.message ? e.message : e));
              }}
            }};
          }}
        }}
      }}

      function render() {{
        renderList();
        renderDetail();
      }}

      function applyEntityUpdate(e) {{
        if (!e || String(e.type || '').toLowerCase() !== 'schedulers') return false;
        const id = String(e.id ?? '');
        if (!id) return false;
        const merged = Object.assign({{}}, e.static || {{}}, e.realtime || {{}});
        merged.ID = merged.ID ?? e.id;
        timersById.set(id, merged);
        if (!ids.includes(id)) ids.push(id);
        return true;
      }}

      async function refreshOnce() {{
        try {{
          const res = await fetch(apiUrl('/api/entities'), {{ cache: 'no-store' }});
          if (!res.ok) return;
          const snap = await res.json();
          const m = snap.meta && typeof snap.meta === 'object' ? snap.meta : null;
          const ts = m && m.last_update ? Number(m.last_update) : 0;
          document.getElementById('lastUpdate').textContent = ts ? new Date(ts * 1000).toISOString().replace('T',' ').slice(0,19) : '-';
          const ents = Array.isArray(snap.entities) ? snap.entities : [];
          let changed = false;
          for (const e of ents) {{
            if (applyEntityUpdate(e)) changed = true;
            if (e && String(e.type || '').toLowerCase() === 'scenarios') {{
              const sid = String(e.id ?? '');
              const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
              const nm = st.DES || st.NM || e.name || sid;
              if (sid) scenarios[sid] = String(nm || sid);
            }}
          }}
          if (changed) {{
            ids.sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
            document.getElementById('count').textContent = String(ids.length);
            render();
          }}
        }} catch (_e) {{}}
      }}

      function startSSE() {{
        try {{ sse = new EventSource(apiUrl('/api/stream')); }} catch (_e) {{ sse = null; return false; }}
        sse.onmessage = (ev) => {{
          let msg = null;
          try {{ msg = JSON.parse(ev.data || '{{}}'); }} catch (_e) {{ msg = null; }}
          if (!msg) return;
          const meta = msg.meta || null;
          if (meta && meta.last_update) {{
            document.getElementById('lastUpdate').textContent = new Date(Number(meta.last_update) * 1000).toISOString().replace('T',' ').slice(0,19);
          }}
          const ents = Array.isArray(msg.entities) ? msg.entities : [];
          let changed = false;
          for (const e of ents) {{
            if (applyEntityUpdate(e)) changed = true;
            if (e && String(e.type || '').toLowerCase() === 'scenarios') {{
              const sid = String(e.id ?? '');
              const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
              const nm = st.DES || st.NM || e.name || sid;
              if (sid) scenarios[sid] = String(nm || sid);
            }}
          }}
          if (changed) {{
            ids.sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
            document.getElementById('count').textContent = String(ids.length);
            render();
          }}
        }};
        sse.onerror = () => {{ try {{ if (sse) sse.close(); }} catch (_e) {{}}; sse = null; }};
        return true;
      }}

      document.getElementById('q').addEventListener('input', () => render());
      document.getElementById('onlyOn').addEventListener('change', () => render());
      parseInit();
      render();
      startSSE();
      setInterval(refreshOnce, 15000);
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_security_functions(snapshot):
    entities = snapshot.get("entities") or []
    ui_tags = _load_ui_tags()
    tag_stats = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "outputs":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        try:
            oid = int(e.get("id"))
        except Exception:
            continue
        tag, visible = _get_ui_tag(ui_tags, "outputs", oid)
        if not visible:
            continue
        tag_key = tag or "Senza tag"
        info = tag_stats.setdefault(tag_key, {"total": 0, "on": 0, "roll": 0})
        cat = str(st.get("CAT") or st.get("TYP") or "").strip().upper()
        status = str(rt.get("STA") or "").strip().upper()
        if cat == "ROLL":
            info["roll"] += 1
        else:
            info["total"] += 1
            if status in ("ON", "1", "TRUE", "T"):
                info["on"] += 1

    tag_items = []
    for tag_name, info in tag_stats.items():
        slug = _slugify_tag(tag_name)
        on = int(info.get("on") or 0)
        total = int(info.get("total") or 0)
        roll = int(info.get("roll") or 0)
        if total > 0:
            meta = f"{on}/{total} ON"
            active = "1" if on > 0 else "0"
        else:
            meta = f"{roll} ROLL" if roll else "0"
            active = "0"
        tag_items.append(
            {
                "tag": tag_name,
                "slug": slug,
                "meta": meta,
                "active": active,
            }
        )
    tag_items.sort(key=lambda x: (x["tag"] == "Senza tag", str(x["tag"]).lower()))

    tag_items_html = ""
    for it in tag_items:
        tag_items_html += (
            f"<a class=\"item tag\" data-tag=\"{_html_escape(it['tag'])}\" data-slug=\"{_html_escape(it['slug'])}\" "
            f"data-active=\"{_html_escape(it['active'])}\" href=\"/security/functions/outputs#tag-{_html_escape(it['slug'])}\">"
            "<div class=\"left\">"
            "<div class=\"icon\">"
            "<svg class=\"tagIcon\" width=\"22\" height=\"22\" viewBox=\"0 0 24 24\" fill=\"currentColor\" aria-hidden=\"true\"></svg>"
            "</div>"
            "<div>"
            f"<div class=\"name\">{_html_escape(it['tag'])}</div>"
            f"<div class=\"meta\">{_html_escape(it['meta'])}</div>"
            "</div>"
            "</div>"
            "<svg class=\"chev\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\">"
            "<path d=\"M9 6l6 6-6 6\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>"
            "</svg>"
            "</a>"
        )

    html = """<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Funzioni</title>
    <style>
      :root { --bg0:#05070b; --fg:#e7eaf0; --muted:rgba(255,255,255,0.6); --border:rgba(255,255,255,0.10); --item:rgba(255,255,255,0.06); }
      html,body { height:100%; }
      body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; color: var(--fg); background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%); }
      .bg { position:fixed; inset:0; background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55)); filter: blur(28px); opacity: 0.85; pointer-events:none; }
      .topbar { position:sticky; top:0; left:0; right:0; display:flex; align-items:center; justify-content:center; gap:18px; height:72px; background: rgba(0,0,0,0.55); backdrop-filter: blur(10px); z-index:2; border-bottom: 1px solid rgba(255,255,255,0.06); }
      .back { position:absolute; left:10px; top:50%; transform:translateY(-50%); display:inline-flex; align-items:center; justify-content:center; width:44px; height:44px; border-radius:999px; border:1px solid rgba(255,255,255,0.10); background: rgba(0,0,0,0.20); color: rgba(255,255,255,0.88); text-decoration:none; }
      .tab { font-size:18px; letter-spacing:0.5px; color:rgba(255,255,255,0.75); text-decoration:none; padding:10px 14px; border-radius:12px; }
      .tab.active { color:#fff; }
      .wrap { max-width:720px; margin:0 auto; padding:88px 18px 32px; }
      .title { font-size:22px; margin:4px 0 14px; color:rgba(255,255,255,0.9); }
      .list { display:flex; flex-direction:column; gap:12px; }
      .item { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 16px; border-radius:14px; background: var(--item); border:1px solid var(--border); text-decoration:none; color: var(--fg); }
      #outputTagContainer { display: contents; }
      .item.tag { padding: 12px 16px; }
      .item.tag .icon { width:34px; height:34px; border-radius:10px; }
      .item[data-active="1"] .icon { border-color: rgba(80, 255, 140, 0.45); box-shadow: 0 0 0 1px rgba(80, 255, 140, 0.10) inset; }
      .left { display:flex; align-items:center; gap:12px; }
      .icon { width:36px; height:36px; border-radius:10px; display:flex; align-items:center; justify-content:center; background: rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.10); }
      .name { font-size:16px; }
      .meta { font-size:12px; color: var(--muted); margin-top:2px; }
      .chev { width:20px; height:20px; color:rgba(255,255,255,0.6); }
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <a class="tab" href="/security">Stato</a>
      <a class="tab" href="/security/partitions">Partizioni</a>
      <a class="tab" href="/security/sensors">Sensori</a>
      <a class="tab active" href="/security/functions">Funzioni</a>
    </div>

    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      <div class="title">Smart Home</div>
      <div class="list">
        <a class="item" href="/security/functions/outputs">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M7 7h10v10H7z" stroke="currentColor" stroke-width="1.6"/>
                <path d="M5 12h2M17 12h2M12 5v2M12 17v2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Output</div>
              <div class="meta">Luci, cancello, tapparelle (da tag)</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <div id="outputTagContainer"><!-- OUTPUT_TAG_ITEMS --></div>

        <a class="item" href="/security/scenarios">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M5 5h6v6H5zM13 5h6v6h-6zM5 13h6v6H5zM13 13h6v6h-6z" stroke="currentColor" stroke-width="1.6"/>
              </svg>
            </div>
            <div>
              <div class="name">Scenari</div>
              <div class="meta">Solo scenari smarthome</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="/thermostats">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M12 3a3 3 0 0 0-3 3v8a4 4 0 1 0 6 0V6a3 3 0 0 0-3-3z" stroke="currentColor" stroke-width="1.6"/>
                <path d="M12 7v7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Termostati</div>
              <div class="meta">Dettagli e profili</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="/security/timers">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.6"/>
                <path d="M12 8v5l3 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Programmatori orari</div>
              <div class="meta">Timer e schedulazioni</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="/logs">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M6 4h9l3 3v13H6z" stroke="currentColor" stroke-width="1.6"/>
                <path d="M9 10h6M9 14h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Registro eventi</div>
              <div class="meta">Log completo</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="/security/reset">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M4 12a8 8 0 1 0 2.3-5.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
                <path d="M4 4v5h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Reset rapidi</div>
              <div class="meta">Cicli, comunicazioni, guasti</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="/security/info">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <rect x="6.5" y="3.5" width="11" height="17" rx="2" stroke="currentColor" stroke-width="1.6"/>
                <path d="M10 17h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Info</div>
              <div class="meta">GSM, temperature, versione</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="/security/users">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4z" stroke="currentColor" stroke-width="1.6"/>
                <path d="M4 20a8 8 0 0 1 16 0" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">Gestione utenti</div>
              <div class="meta">Abilita/disabilita utenti</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>
      </div>
    </div>

    <script>
      function iconKeyFallback(tag) {
        const s = String(tag || '').toLowerCase();
        if (s.includes('luc')) return 'mdiLightbulb';
        if (s.includes('canc') || s.includes('gate')) return 'mdiGate';
        if (s.includes('bar')) return 'mdiBoomGate';
        if (s.includes('tapp') || s.includes('shutter')) return 'mdiWindowShutter';
        if (s.includes('tenda') || s.includes('curtain')) return 'mdiCurtainsClosed';
        if (s.includes('roll')) return 'mdiRollerShadeClosed';
        if (s.includes('blind')) return 'mdiBlindsHorizontalClosed';
        if (s.includes('pump') || s.includes('pompa')) return 'mdiPump';
        return 'mdiGridLarge';
      }

      const ICONS = {
        // Official MDI SVG paths (MaterialDesignIcons.com / Templarian MaterialDesign repo).
        mdiPump: '<path d="M2 21V15H3.5C3.18 14.06 3 13.05 3 12C3 7.03 7.03 3 12 3H22V9H20.5C20.82 9.94 21 10.95 21 12C21 16.97 16.97 21 12 21H2M5 12C5 13.28 5.34 14.47 5.94 15.5L9.4 13.5C9.15 13.06 9 12.55 9 12C9 11.35 9.21 10.75 9.56 10.26L6.3 7.93C5.5 9.08 5 10.5 5 12M12 19C14.59 19 16.85 17.59 18.06 15.5L14.6 13.5C14.08 14.4 13.11 15 12 15L11.71 15L11.33 18.97L12 19M12 9C13.21 9 14.26 9.72 14.73 10.76L18.37 9.1C17.27 6.68 14.83 5 12 5V9M12 11C11.45 11 11 11.45 11 12C11 12.55 11.45 13 12 13C12.55 13 13 12.55 13 12C13 11.45 12.55 11 12 11Z" />',
        mdiRollerShade: '<path d="M20 19V3H4V19H2V21H22V19H20M6 19V13H11V14.8C10.6 15.1 10.2 15.6 10.2 16.2C10.2 17.2 11 18 12 18S13.8 17.2 13.8 16.2C13.8 15.6 13.5 15.1 13 14.8V13H18V19H6Z" />',
        mdiBlindsHorizontal: '<path d="M20 19V3H4V19H2V21H22V19H20M16 9H18V11H16V9M14 11H6V9H14V11M18 7H16V5H18V7M14 5V7H6V5H14M6 19V13H14V14.82C13.55 15.14 13.25 15.66 13.25 16.25C13.25 17.22 14.03 18 15 18S16.75 17.22 16.75 16.25C16.75 15.66 16.45 15.13 16 14.82V13H18V19H6Z" />',
        mdiWindowShutterOpen: '<path d="M3 4H21V8H19V20H17V8H7V20H5V8H3V4M8 9H16V11H8V9Z" />',
        mdiBlindsHorizontalClosed: '<path d="M20 19V3H4V19H2V21H13.25C13.25 21.97 14.03 22.75 15 22.75S16.75 21.97 16.75 21H22V19H20M18 11H16V9H18V11M14 11H6V9H14V11M14 13V15H6V13H14M16 13H18V15H16V13M18 7H16V5H18V7M14 5V7H6V5H14M6 19V17H14V19H6M16 19V17H18V19H16Z" />',
        mdiWindowShutter: '<path d="M3 4H21V8H19V20H17V8H7V20H5V8H3V4M8 9H16V11H8V9M8 12H16V14H8V12M8 15H16V17H8V15M8 18H16V20H8V18Z" />',
        mdiBoomGate: '<path d="M20,9H8.22C7.11,7.77 5.21,7.68 4,8.8C3.36,9.36 3,10.16 3,11V20A1,1 0 0,0 2,21V22H10V21A1,1 0 0,0 9,20V13H20A2,2 0 0,0 22,11A2,2 0 0,0 20,9M6,12.5A1.5,1.5 0 0,1 4.5,11A1.5,1.5 0 0,1 6,9.5A1.5,1.5 0 0,1 7.5,11A1.5,1.5 0 0,1 6,12.5M10.5,12L9,10H10.5L12,12H10.5M14.5,12L13,10H14.5L16,12H14.5M18.5,12L17,10H18.5L20,12H18.5Z" />',
        mdiCurtains: '<path d="M23 3H1V1H23V3M2 22H6C6 19 4 17 4 17C10 13 11 4 11 4H2V22M22 4H13C13 4 14 13 20 17C20 17 18 19 18 22H22V4Z" />',
        mdiGarageOpenVariant: '<path d="M22 9V20H20V11H4V20H2V9L12 5L22 9M19 12H5V14H19V12Z" />',
        mdiLightbulb: '<path d="M12,2A7,7 0 0,0 5,9C5,11.38 6.19,13.47 8,14.74V17A1,1 0 0,0 9,18H15A1,1 0 0,0 16,17V14.74C17.81,13.47 19,11.38 19,9A7,7 0 0,0 12,2M9,21A1,1 0 0,0 10,22H14A1,1 0 0,0 15,21V20H9V21Z" />',
        mdiGarageVariant: '<path d="M22 9V20H20V11H4V20H2V9L12 5L22 9M19 12H5V14H19V12M19 18H5V20H19V18M19 15H5V17H19V15Z" />',
        mdiRollerShadeClosed: '<path d="M20 19V3H4V19H2V21H10.25C10.25 21.97 11.03 22.75 12 22.75S13.75 21.97 13.75 21H22V19H20M6 19V17H11V19H6M13 19V17H18V19H13Z" />',
        mdiGate: '<path d="M9 6V11H7V7H5V11H3V9H1V21H3V19H5V21H7V19H9V21H11V19H13V21H15V19H17V21H19V19H21V21H23V9H21V11H19V7H17V11H15V6H13V11H11V6H9M3 13H5V17H3V13M7 13H9V17H7V13M11 13H13V17H11V13M15 13H17V17H15V13M19 13H21V17H19V13Z" />',
        mdiGridLarge: '<path d="M4,2H20A2,2 0 0,1 22,4V20A2,2 0 0,1 20,22H4C2.92,22 2,21.1 2,20V4A2,2 0 0,1 4,2M4,4V11H11V4H4M4,20H11V13H4V20M20,20V13H13V20H20M20,4H13V11H20V4Z" />',
        mdiCurtainsClosed: '<path d="M23 3H1V1H23V3M2 22H11V4H2V22M22 4H13V22H22V4Z" />',
      };

      function getTagStyle(tag, tags) {
        const styles = (tags && tags.tag_styles && typeof tags.tag_styles === 'object') ? tags.tag_styles : {};
        const t = String(tag || '').trim();
        const s = styles[t];
        if (!s || typeof s !== 'object') return null;
        return {
          icon_off: String(s.icon_off || '').trim(),
          icon_on: String(s.icon_on || '').trim(),
          color_off: String(s.color_off || '').trim(),
          color_on: String(s.color_on || '').trim(),
          svg_off: String(s.svg_off || '').trim(),
          svg_on: String(s.svg_on || '').trim(),
        };
      }

      function setTagIcons(scope, tags) {
        const root = scope || document;
        for (const a of root.querySelectorAll('a.item.tag')) {
          const tag = a.getAttribute('data-tag') || '';
          const active = String(a.getAttribute('data-active') || '') === '1';
          const style = getTagStyle(tag, tags);
          const iconKey = style ? (active ? (style.icon_on || style.icon_off) : style.icon_off) : iconKeyFallback(tag);
          const svg = a.querySelector('svg.tagIcon');
          if (svg) {
            const custom = style ? (active ? (style.svg_on || '') : (style.svg_off || '')) : '';
            svg.innerHTML = custom || ICONS[iconKey] || ICONS.mdiGridLarge;
          }
          const color = style ? (active ? (style.color_on || '') : (style.color_off || '')) : '';
          const iconWrap = a.querySelector('.icon');
          if (iconWrap) iconWrap.style.color = color || '';
        }
      }

      function escapeHtml(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      }

      function slugifyTag(tag) {
        const s = String(tag || '').trim().toLowerCase();
        let out = '';
        let prevDash = false;
        for (const ch of s) {
          const ok = /[a-z0-9]/.test(ch);
          if (ok) { out += ch; prevDash = false; continue; }
          if (!prevDash) { out += '-'; prevDash = true; }
        }
        out = out.replace(/^-+|-+$/g,'');
        return out || 'senza-tag';
      }

      async function loadAll() {
        const [entitiesRes, tagsRes] = await Promise.allSettled([
          fetch('/api/entities', { cache: 'no-store' }),
          fetch('/api/ui_tags', { cache: 'no-store' }),
        ]);
        const entities = (entitiesRes.status === 'fulfilled' && entitiesRes.value.ok) ? await entitiesRes.value.json() : [];
        const tags = (tagsRes.status === 'fulfilled' && tagsRes.value.ok) ? await tagsRes.value.json() : {};
        return { entities: Array.isArray(entities) ? entities : [], tags: (tags && typeof tags === 'object') ? tags : {} };
      }

      function computeTagStats(entities, tags) {
        const map = (tags && tags.outputs && typeof tags.outputs === 'object') ? tags.outputs : {};
        const stats = new Map();
        for (const e of (entities || [])) {
          if (String(e.type || '').toLowerCase() !== 'outputs') continue;
          const id = Number(e.id);
          if (!Number.isFinite(id)) continue;
          const entry = map[String(id)];
          const visible = (entry && typeof entry === 'object' && entry.visible === false) ? false : true;
          if (!visible) continue;
          const tag = (entry && typeof entry === 'object' && typeof entry.tag === 'string') ? entry.tag.trim() : '';
          const tagKey = tag || 'Senza tag';
          const st = (e.static && typeof e.static === 'object') ? e.static : {};
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {};
          const cat = String(st.CAT || st.TYP || '').trim().toUpperCase();
          const sta = String(rt.STA || '').trim().toUpperCase();
          const pos = (rt.POS !== undefined && rt.POS !== null) ? String(rt.POS) : '';
          const row = stats.get(tagKey) || { on: 0, total: 0, roll: 0 };
          const isOn = (() => {
            if (sta === 'ON' || sta === '1' || sta === 'TRUE' || sta === 'T' || sta === 'OPEN' || sta === 'OPENING' || sta === 'OP') return true;
            if (sta === 'OFF' || sta === '0' || sta === 'FALSE' || sta === 'F' || sta === 'CLOSE' || sta === 'CLOSED' || sta === 'CLOSING' || sta === 'CL') return false;
            const n = Number(String(pos ?? '').trim());
            return Number.isFinite(n) && n > 0;
          })();
          row.total += 1;
          if (cat === 'ROLL') row.roll += 1;
          if (isOn) row.on += 1;
          stats.set(tagKey, row);
        }
        return stats;
      }

      function renderTagItems(stats) {
        const items = [];
        for (const [tag, row] of stats.entries()) {
          const total = Number(row.total || 0);
          const on = Number(row.on || 0);
          const roll = Number(row.roll || 0);
          const meta = total > 0 ? `${on}/${total} ON` : (roll ? `${roll} ROLL` : '0');
          const active = (total > 0 && on > 0) ? '1' : '0';
          const slug = slugifyTag(tag);
          items.push({ tag, slug, meta, active });
        }
        items.sort((a,b) => (a.tag === 'Senza tag') - (b.tag === 'Senza tag') || a.tag.localeCompare(b.tag));
        return items.map(it => (
          `<a class="item tag" data-tag="${escapeHtml(it.tag)}" data-slug="${escapeHtml(it.slug)}" data-active="${it.active}" href="/security/functions/outputs#tag-${escapeHtml(it.slug)}">` +
            '<div class="left">' +
              '<div class="icon"><svg class="tagIcon" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"></svg></div>' +
              `<div><div class="name">${escapeHtml(it.tag)}</div><div class="meta">${escapeHtml(it.meta)}</div></div>` +
            '</div>' +
            '<svg class="chev" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
          '</a>'
        )).join('');
      }

      async function refresh() {
        try {
          const box = document.getElementById('outputTagContainer');
          if (!box) return;
          const data = await loadAll();
          const stats = computeTagStats(data.entities, data.tags);
          box.innerHTML = renderTagItems(stats);
          setTagIcons(box, data.tags);
        } catch (_e) {}
      }

      let sse = null;
      let refreshTimer = null;
      function scheduleRefresh() {
        if (refreshTimer) return;
        refreshTimer = setTimeout(() => { refreshTimer = null; refresh(); }, 350);
      }
      function startSSE() {
        try { sse = new EventSource('/api/stream'); } catch (_e) { sse = null; return false; }
        sse.onmessage = () => scheduleRefresh();
        sse.onerror = () => { try { if (sse) sse.close(); } catch (_e) {} sse = null; };
        return true;
      }

      refresh();
      startSSE();
      setInterval(refresh, 15000);
    </script>
  </body>
</html>"""
    html = html.replace("<!-- OUTPUT_TAG_ITEMS -->", tag_items_html or "")
    return html.encode("utf-8")


def render_security_functions_outputs(snapshot):
    entities = snapshot.get("entities") or []
    ui_tags = _load_ui_tags()
    groups = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "outputs":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        try:
            oid = int(e.get("id"))
        except Exception:
            continue
        tag, visible = _get_ui_tag(ui_tags, "outputs", oid)
        if not visible:
            continue
        tag_key = tag or "Senza tag"
        groups.setdefault(tag_key, []).append(
            {
                "ID": oid,
                "DES": st.get("DES") or e.get("name") or f"Output {oid}",
                "CAT": str(st.get("CAT") or st.get("TYP") or "").strip().upper(),
                "STA": str(rt.get("STA") or "").strip(),
                "POS": rt.get("POS"),
            }
        )
    for items in groups.values():
        items.sort(key=lambda x: (str(x.get("DES") or ""), str(x.get("ID") or "")))
    group_keys = sorted(groups.keys(), key=lambda x: (x == "Senza tag", x.lower()))

    sections = []
    empty_rows_html = "<div class=\"row\"><div class=\"name\">Nessun output</div></div>"
    def _is_output_on(cat: str, status: str, pos) -> bool:
        cl = str(cat or "").strip().upper()
        st = str(status or "").strip().upper()
        if st in ("ON", "1", "TRUE", "T"):
            return True
        if st in ("OPEN", "OPENING", "OP"):
            return True
        if st in ("CLOSE", "CLOSED", "CLOSING", "CL", "OFF", "0", "FALSE", "F"):
            return False
        if cl == "ROLL":
            try:
                n = float(str(pos).strip())
            except Exception:
                n = None
            if n is None:
                return False
            # ROLL: 0=chiuso, >0=aperto (es.: POS 0..255 o 0..100)
            return n > 0
        return False
    for g in group_keys:
        items = groups.get(g) or []
        slug = _slugify_tag(g)
        gl = str(g or "").lower()
        group_kind = "light" if "luc" in gl else ("gate" if ("canc" in gl or "gate" in gl) else ("blinds" if ("tapp" in gl or "avvolg" in gl or "roll" in gl) else "grid"))
        group_icon = _icon_svg(group_kind)
        group_any_on = False
        rows = []
        for it in items:
            cat = it.get("CAT") or ""
            is_roll = cat == "ROLL"
            status = it.get("STA") or ""
            pos = it.get("POS")
            cl = str(cat or "").strip().upper()
            if cl == "ROLL":
                kind = "blinds"
            elif cl == "LIGHT":
                kind = "light"
            else:
                kind = group_kind
            icon = _icon_svg(kind)
            is_on = _is_output_on(cat, status, pos)
            if is_on:
                group_any_on = True
            meta = []
            if cat:
                meta.append(cat)
            if status:
                meta.append(f"STA {status}")
            if pos not in (None, ""):
                meta.append(f"POS {pos}")
            meta_txt = " · ".join(meta)
            if is_roll:
                actions = (
                    f"<button class=\"btn\" data-oid=\"{_html_escape(str(it['ID']))}\" data-act=\"up\">UP</button>"
                    f"<button class=\"btn\" data-oid=\"{_html_escape(str(it['ID']))}\" data-act=\"down\">DOWN</button>"
                    f"<button class=\"btn\" data-oid=\"{_html_escape(str(it['ID']))}\" data-act=\"stop\">STOP</button>"
                )
            else:
                actions = (
                    f"<button class=\"btn\" data-oid=\"{_html_escape(str(it['ID']))}\" data-act=\"on\">ON</button>"
                    f"<button class=\"btn\" data-oid=\"{_html_escape(str(it['ID']))}\" data-act=\"off\">OFF</button>"
                    f"<button class=\"btn\" data-oid=\"{_html_escape(str(it['ID']))}\" data-act=\"toggle\">TOGGLE</button>"
                )
            rows.append(
                f"<div class=\"row\" data-tag=\"{_html_escape(str(g))}\">"
                f"<div><div class=\"name\"><span class=\"icoInline{(' on' if is_on else '')}\" data-tag=\"{_html_escape(str(g))}\">{icon}</span>{_html_escape(it['DES'])}</div>"
                f"<div class=\"meta\">ID {_html_escape(str(it['ID']))}{(' · ' + _html_escape(meta_txt)) if meta_txt else ''}</div></div>"
                f"<div class=\"actions\">{actions}</div>"
                "</div>"
            )
        sections.append(
            f"<div class=\"group\" id=\"tag-{_html_escape(slug)}\" data-tag=\"{_html_escape(str(g))}\"><div class=\"groupTitle\"><span class=\"gico{(' on' if group_any_on else '')}\" data-tag=\"{_html_escape(str(g))}\">{group_icon}</span>{_html_escape(g)}</div>"
            f"<div class=\"list\">{''.join(rows) or empty_rows_html}</div></div>"
        )

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Output</title>
    <style>
      :root {{
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: rgba(255,255,255,0.12);
        --card2: rgba(255,255,255,0.04);
      }}
      html,body {{ height:100%; }}
      body {{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }}
      .bg {{
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }}
      .topbar {{
        position:sticky; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center; gap:18px;
        height:72px;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(10px);
        z-index: 2;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .back {{
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.20);
        color: rgba(255,255,255,0.88);
        text-decoration: none;
      }}
      .wrap {{
        max-width: 1020px;
        margin: 0 auto;
        padding: 90px 18px 32px;
      }}
      .groupTitle {{
        margin: 16px 0 8px;
        font-size: 18px;
        color: rgba(255,255,255,0.9);
      }}
      .gico {{
        display:inline-flex;
        align-items:center;
        justify-content:center;
        width: 30px;
        height: 30px;
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.12);
        background: rgba(255,255,255,0.03);
      }}
      .gico.on {{
        color: #ffd24a;
        border-color: rgba(255, 210, 74, 0.45);
        box-shadow: 0 0 0 1px rgba(255, 210, 74, 0.10) inset;
      }}
      .list {{
        display:flex;
        flex-direction: column;
        gap: 10px;
      }}
      .row {{
        display:flex;
        align-items:center;
        justify-content: space-between;
        gap: 10px;
        padding: 10px 12px;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        background: rgba(255,255,255,0.04);
      }}
      .row .name {{ font-size: 15px; display:flex; align-items:center; gap:8px; }}
      .row .meta {{ color: var(--muted); font-size: 12px; }}
      .row .actions {{ display:flex; gap:8px; flex-wrap: wrap; justify-content: flex-end; }}
      .icoInline {{
        width: 28px;
        height: 28px;
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.12);
        background: rgba(255,255,255,0.03);
        display:inline-flex;
        align-items:center;
        justify-content:center;
        flex: 0 0 auto;
      }}
      .icoInline.on {{
        color: #ffd24a;
        border-color: rgba(255, 210, 74, 0.45);
        box-shadow: 0 0 0 1px rgba(255, 210, 74, 0.10) inset;
      }}
      .icoInline svg {{ display:block; }}
      .btn {{
        display:inline-flex;
        align-items:center;
        gap:8px;
        padding: 6px 10px;
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.14);
        background: var(--card2);
        color: var(--fg);
        cursor: pointer;
        text-decoration: none;
        font-size: 12px;
      }}
      .btn:hover {{ border-color: rgba(255,255,255,0.28); }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/security/functions" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <span class="groupTitle">Output</span>
    </div>

    <div class="wrap">
      <div style="display:flex;align-items:center;justify-content:flex-start;margin:0 0 10px 0;">
        <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
      </div>
      {''.join(sections) or empty_rows_html}
    </div>

    <script>
      (function() {{
        try {{
          if (window.location && window.location.hash) {{
            const el = document.querySelector(window.location.hash);
            if (el && el.scrollIntoView) el.scrollIntoView({{ block: 'start', behavior: 'smooth' }});
          }}
        }} catch (_e) {{}}
      }})();
      async function sendCmd(type, id, action) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        const res = await fetch('/api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const text = await res.text();
        return {{ ok: res.ok, text }};
      }}

      async function fetchSnap() {{
        const res = await fetch('/api/entities', {{ cache: 'no-store' }});
        return await res.json();
      }}

      async function fetchUiTags() {{
        const candidates = ['api/ui_tags','./api/ui_tags','../api/ui_tags','../../api/ui_tags','../../../api/ui_tags','/api/ui_tags'];
        for (const url of candidates) {{
          try {{
            const res = await fetch(url, {{ cache: 'no-store' }});
            if (res.ok) return await res.json();
          }} catch (_e) {{}}
        }}
        return {{}};
      }}

      const TAG_STYLE_ICONS = {{
        // Official MDI SVG paths (MaterialDesignIcons.com / Templarian MaterialDesign repo).
        mdiGate: '<path d="M9 6V11H7V7H5V11H3V9H1V21H3V19H5V21H7V19H9V21H11V19H13V21H15V19H17V21H19V19H21V21H23V9H21V11H19V7H17V11H15V6H13V11H11V6H9M3 13H5V17H3V13M7 13H9V17H7V13M11 13H13V17H11V13M15 13H17V17H15V13M19 13H21V17H19V13Z" />',
        mdiBoomGate: '<path d="M20,9H8.22C7.11,7.77 5.21,7.68 4,8.8C3.36,9.36 3,10.16 3,11V20A1,1 0 0,0 2,21V22H10V21A1,1 0 0,0 9,20V13H20A2,2 0 0,0 22,11A2,2 0 0,0 20,9M6,12.5A1.5,1.5 0 0,1 4.5,11A1.5,1.5 0 0,1 6,9.5A1.5,1.5 0 0,1 7.5,11A1.5,1.5 0 0,1 6,12.5M10.5,12L9,10H10.5L12,12H10.5M14.5,12L13,10H14.5L16,12H14.5M18.5,12L17,10H18.5L20,12H18.5Z" />',
        mdiGarageVariant: '<path d="M22 9V20H20V11H4V20H2V9L12 5L22 9M19 12H5V14H19V12M19 18H5V20H19V18M19 15H5V17H19V15Z" />',
        mdiGarageOpenVariant: '<path d="M22 9V20H20V11H4V20H2V9L12 5L22 9M19 12H5V14H19V12Z" />',
        mdiGridLarge: '<path d="M4,2H20A2,2 0 0,1 22,4V20A2,2 0 0,1 20,22H4C2.92,22 2,21.1 2,20V4A2,2 0 0,1 4,2M4,4V11H11V4H4M4,20H11V13H4V20M20,20V13H13V20H20M20,4H13V11H20V4Z" />',
        mdiCurtainsClosed: '<path d="M23 3H1V1H23V3M2 22H11V4H2V22M22 4H13V22H22V4Z" />',
        mdiCurtains: '<path d="M23 3H1V1H23V3M2 22H6C6 19 4 17 4 17C10 13 11 4 11 4H2V22M22 4H13C13 4 14 13 20 17C20 17 18 19 18 22H22V4Z" />',
        mdiLightbulb: '<path d="M12,2A7,7 0 0,0 5,9C5,11.38 6.19,13.47 8,14.74V17A1,1 0 0,0 9,18H15A1,1 0 0,0 16,17V14.74C17.81,13.47 19,11.38 19,9A7,7 0 0,0 12,2M9,21A1,1 0 0,0 10,22H14A1,1 0 0,0 15,21V20H9V21Z" />',
        mdiBlindsHorizontalClosed: '<path d="M20 19V3H4V19H2V21H13.25C13.25 21.97 14.03 22.75 15 22.75S16.75 21.97 16.75 21H22V19H20M18 11H16V9H18V11M14 11H6V9H14V11M14 13V15H6V13H14M16 13H18V15H16V13M18 7H16V5H18V7M14 5V7H6V5H14M6 19V17H14V19H6M16 19V17H18V19H16Z" />',
        mdiBlindsHorizontal: '<path d="M20 19V3H4V19H2V21H22V19H20M16 9H18V11H16V9M14 11H6V9H14V11M18 7H16V5H18V7M14 5V7H6V5H14M6 19V13H14V14.82C13.55 15.14 13.25 15.66 13.25 16.25C13.25 17.22 14.03 18 15 18S16.75 17.22 16.75 16.25C16.75 15.66 16.45 15.13 16 14.82V13H18V19H6Z" />',
        mdiRollerShadeClosed: '<path d="M20 19V3H4V19H2V21H10.25C10.25 21.97 11.03 22.75 12 22.75S13.75 21.97 13.75 21H22V19H20M6 19V17H11V19H6M13 19V17H18V19H13Z" />',
        mdiRollerShade: '<path d="M20 19V3H4V19H2V21H22V19H20M6 19V13H11V14.8C10.6 15.1 10.2 15.6 10.2 16.2C10.2 17.2 11 18 12 18S13.8 17.2 13.8 16.2C13.8 15.6 13.5 15.1 13 14.8V13H18V19H6Z" />',
        mdiWindowShutter: '<path d="M3 4H21V8H19V20H17V8H7V20H5V8H3V4M8 9H16V11H8V9M8 12H16V14H8V12M8 15H16V17H8V15M8 18H16V20H8V18Z" />',
        mdiWindowShutterOpen: '<path d="M3 4H21V8H19V20H17V8H7V20H5V8H3V4M8 9H16V11H8V9Z" />',
        mdiPump: '<path d="M2 21V15H3.5C3.18 14.06 3 13.05 3 12C3 7.03 7.03 3 12 3H22V9H20.5C20.82 9.94 21 10.95 21 12C21 16.97 16.97 21 12 21H2M5 12C5 13.28 5.34 14.47 5.94 15.5L9.4 13.5C9.15 13.06 9 12.55 9 12C9 11.35 9.21 10.75 9.56 10.26L6.3 7.93C5.5 9.08 5 10.5 5 12M12 19C14.59 19 16.85 17.59 18.06 15.5L14.6 13.5C14.08 14.4 13.11 15 12 15L11.71 15L11.33 18.97L12 19M12 9C13.21 9 14.26 9.72 14.73 10.76L18.37 9.1C17.27 6.68 14.83 5 12 5V9M12 11C11.45 11 11 11.45 11 12C11 12.55 11.45 13 12 13C12.55 13 13 12.55 13 12C13 11.45 12.55 11 12 11Z" />',
      }};

      let TAG_STYLES = null;
      async function ensureTagStyles() {{
        if (TAG_STYLES !== null) return TAG_STYLES;
        try {{
          const tags = await fetchUiTags();
          TAG_STYLES = (tags && tags.tag_styles && typeof tags.tag_styles === 'object') ? tags.tag_styles : {{}};
        }} catch (_e) {{
          TAG_STYLES = {{}};
        }}
        return TAG_STYLES;
      }}

      function isOutputOn(cat, sta, pos) {{
        const cl = String(cat || '').trim().toUpperCase();
        const st = String(sta || '').trim().toUpperCase();
        if (['ON','1','TRUE','T','OPEN','OPENING','OP'].includes(st)) return true;
        if (['OFF','0','FALSE','F','CLOSE','CLOSED','CLOSING','CL'].includes(st)) return false;
        if (cl === 'ROLL') {{
          const n = Number(String(pos ?? '').trim());
          return Number.isFinite(n) && n > 0;
        }}
        // Alcune uscite (es. portoni) non usano ON/OFF ma POS (0=chiuso, >0=aperto).
        if (cl !== 'LIGHT') {{
          const n = Number(String(pos ?? '').trim());
          if (Number.isFinite(n)) return n > 0;
        }}
        return false;
      }}

      function buildOutputsMap(entities) {{
        const out = new Map();
        for (const e of (entities || [])) {{
          if (String(e.type || '').toLowerCase() !== 'outputs') continue;
          const id = Number(e.id);
          if (!Number.isFinite(id)) continue;
          const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
          const rt = (e.realtime && typeof e.realtime === 'object') ? e.realtime : {{}};
          const cat = String(st.CAT || st.TYP || '').trim().toUpperCase();
          const sta = String(rt.STA || '').trim();
          const pos = (rt.POS !== undefined && rt.POS !== null) ? String(rt.POS) : '';
          out.set(String(id), {{ cat, sta, pos }});
        }}
        return out;
      }}

      function applyOutputsState(entities) {{
        const map = buildOutputsMap(entities || []);
        const seen = new Set();
        for (const btn of document.querySelectorAll('button[data-oid]')) {{
          const oid = String(btn.getAttribute('data-oid') || '');
          if (!oid || seen.has(oid)) continue;
          seen.add(oid);
          const row = btn.closest ? btn.closest('.row') : null;
          if (!row) continue;
          const st = map.get(oid) || null;
          if (!st) continue;
          const metaEl = row.querySelector('.meta');
          const ico = row.querySelector('.icoInline');
          const parts = [];
          if (st.cat) parts.push(st.cat);
          if (st.sta) parts.push('STA ' + st.sta);
          if (st.pos) parts.push('POS ' + st.pos);
          if (metaEl) metaEl.textContent = 'ID ' + oid + (parts.length ? (' · ' + parts.join(' · ')) : '');
          if (ico) {{
            const isOn = isOutputOn(st.cat, st.sta, st.pos);
            ico.classList.toggle('on', isOn);

            const tag = String(ico.getAttribute('data-tag') || row.getAttribute('data-tag') || '').trim();
            const styles = (TAG_STYLES && typeof TAG_STYLES === 'object') ? TAG_STYLES : null;
            const stl = (styles && tag && styles[tag] && typeof styles[tag] === 'object') ? styles[tag] : null;
            if (stl) {{
              const custom = String(isOn ? (stl.svg_on || '') : (stl.svg_off || '')).trim();
              const iconKey = String(isOn ? (stl.icon_on || '') : (stl.icon_off || '')).trim();
              const color = String(isOn ? (stl.color_on || '') : (stl.color_off || '')).trim();
              const svgPath = iconKey ? TAG_STYLE_ICONS[iconKey] : null;
              const inner = custom || svgPath || '';
              if (inner) {{
                ico.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${{inner}}</svg>`;
              }}
              ico.style.color = color || '';
            }}
          }}
        }}
        // Update group icons based on any child ON.
        for (const group of document.querySelectorAll('.group')) {{
          const anyOn = !!group.querySelector('.icoInline.on');
          const gico = group.querySelector('.gico');
          if (gico) {{
            gico.classList.toggle('on', anyOn);
            const tag = String(gico.getAttribute('data-tag') || group.getAttribute('data-tag') || '').trim();
            const styles = (TAG_STYLES && typeof TAG_STYLES === 'object') ? TAG_STYLES : null;
            const stl = (styles && tag && styles[tag] && typeof styles[tag] === 'object') ? styles[tag] : null;
            if (stl) {{
              const custom = String(anyOn ? (stl.svg_on || '') : (stl.svg_off || '')).trim();
              const iconKey = String(anyOn ? (stl.icon_on || '') : (stl.icon_off || '')).trim();
              const color = String(anyOn ? (stl.color_on || '') : (stl.color_off || '')).trim();
              const svgPath = iconKey ? TAG_STYLE_ICONS[iconKey] : null;
              const inner = custom || svgPath || '';
              if (inner) {{
                gico.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${{inner}}</svg>`;
              }}
              gico.style.color = color || '';
            }}
          }}
        }}
      }}

      let refreshTimer = null;
      function scheduleRefresh() {{
        if (refreshTimer) return;
        refreshTimer = setTimeout(async () => {{
          refreshTimer = null;
          try {{
            const snap = await fetchSnap();
            await ensureTagStyles();
            applyOutputsState(snap.entities || []);
          }} catch (_e) {{}}
        }}, 300);
      }}

      // Live updates (SSE), fallback polling
      let sse = null;
      function startSSE() {{
        try {{ sse = new EventSource('/api/stream'); }} catch (_e) {{ sse = null; return false; }}
        sse.onmessage = () => scheduleRefresh();
        sse.onerror = () => {{ try {{ if (sse) sse.close(); }} catch (_e) {{}}; sse = null; }};
        return true;
      }}

      for (const btn of document.querySelectorAll('button[data-oid][data-act]')) {{
        btn.addEventListener('click', async (ev) => {{
          const id = Number(ev.currentTarget.getAttribute('data-oid'));
          const act = String(ev.currentTarget.getAttribute('data-act'));
          ev.currentTarget.disabled = true;
          const res = await sendCmd('outputs', id, act);
          ev.currentTarget.disabled = false;
          if (!res.ok) alert('Errore: ' + res.text);
          scheduleRefresh();
        }});
      }}

      startSSE();
      scheduleRefresh();
      setInterval(scheduleRefresh, 5000);
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_menu(snapshot):
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Menu</title>
    <style>
      :root {{ --bg0:#05070b; --fg:#e7eaf0; --muted:rgba(255,255,255,0.65); --border:rgba(255,255,255,0.10); --item:rgba(255,255,255,0.06); }}
      html,body {{ height:100%; }}
      body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; color:var(--fg);
             background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%); }}
      .bg {{ position:fixed; inset:0; background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55)); filter: blur(28px); opacity:0.85; pointer-events:none; }}
      .wrap {{ max-width: 720px; margin: 0 auto; padding: 18px 16px 48px; }}
      .title {{ display:flex; align-items:center; gap:12px; margin: 6px 0 14px; }}
      .title h1 {{ font-size: 20px; margin:0; font-weight: 750; letter-spacing: 0.2px; }}
      .badge {{ display:inline-block; padding: 6px 10px; border-radius: 999px; border:1px solid var(--border); background: rgba(0,0,0,0.25); color: rgba(255,255,255,0.82); font-size: 12px; }}
      .list {{ display:flex; flex-direction:column; gap:12px; margin-top: 14px; }}
      a.item {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 16px; border-radius:14px; background: var(--item); border:1px solid var(--border);
                color:var(--fg); text-decoration:none; }}
      .left {{ display:flex; align-items:center; gap:12px; min-width:0; }}
      .icon {{ width:40px; height:40px; border-radius: 12px; display:flex; align-items:center; justify-content:center; background: rgba(0,0,0,0.25); border:1px solid rgba(255,255,255,0.08); }}
      .name {{ font-size:16px; font-weight: 650; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .meta {{ font-size: 12px; color: var(--muted); margin-top: 4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .chev {{ width: 22px; height: 22px; color: rgba(255,255,255,0.7); }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <div class="wrap">
      <div class="title">
        <img src="assets/logo_ekonex.png" alt="Ekonex" style="height:30px;opacity:0.95;"/>
        <img src="assets/e-safe_scr.png" alt="e-safe" style="height:30px;opacity:0.92;"/>
        <div>
          <h1>Ksenia Lares</h1>
          <div class="badge">v{_html_escape(ADDON_VERSION)}</div>
        </div>
      </div>

      <div class="list">
        <a class="item" href="security">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M12 3l8 6v11H4V9l8-6z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
                <path d="M10 20v-6h4v6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">UI Sicurezza</div>
              <div class="meta">Stato, partizioni, sensori, funzioni</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>

        <a class="item" href="index_debug">
          <div class="left">
            <div class="icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
              </svg>
            </div>
            <div>
              <div class="name">index_debug</div>
              <div class="meta">Debug, snapshot, strumenti</div>
            </div>
          </div>
          <svg class="chev" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </a>
      </div>
    </div>
  </body>
</html>"""
    return html.encode("utf-8")


def render_index_tag_styles(snapshot):
    ui_tags = _load_ui_tags()
    styles = ui_tags.get("tag_styles") if isinstance(ui_tags, dict) else {}
    if not isinstance(styles, dict):
        styles = {}
    initial = json.dumps(styles, ensure_ascii=False)

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Tag styles</title>
    <style>
      :root {{ --bg0:#05070b; --fg:#e7eaf0; --muted:rgba(255,255,255,0.65); --card:rgba(255,255,255,0.06); --border:rgba(255,255,255,0.10); }}
      html,body {{ height:100%; }}
      body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; color: var(--fg); background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%); }}
      .bg {{ position:fixed; inset:0; background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55)); filter: blur(28px); opacity: 0.85; pointer-events:none; }}
      .top {{ position: sticky; top:0; background: rgba(0,0,0,0.55); backdrop-filter: blur(10px); border-bottom: 1px solid rgba(255,255,255,0.06); z-index:2; }}
      .bar {{ max-width: 1100px; margin:0 auto; padding: 14px 16px; display:flex; align-items:center; justify-content:space-between; gap: 12px; }}
      a {{ color: rgba(255,255,255,0.85); text-decoration:none; }}
      a:hover {{ text-decoration: underline; }}
      .btn {{ display:inline-flex; align-items:center; gap:8px; padding: 8px 12px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.90); cursor:pointer; }}
      .btn:hover {{ border-color: rgba(255,255,255,0.28); }}
      .btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
      .wrap {{ max-width: 1100px; margin:0 auto; padding: 18px 16px 48px; }}
      .hint {{ color: var(--muted); font-size: 13px; }}
      table {{ width:100%; border-collapse: collapse; margin-top: 12px; border: 1px solid var(--border); border-radius: 12px; overflow:hidden; }}
      th,td {{ padding: 10px 10px; border-bottom: 1px solid rgba(255,255,255,0.06); vertical-align: middle; }}
      th {{ text-align:left; color: rgba(255,255,255,0.70); font-size: 12px; letter-spacing: 0.8px; text-transform: uppercase; }}
      tr:nth-child(even) td {{ background: rgba(255,255,255,0.03); }}
      input[type="text"] {{ width: 100%; padding: 8px 10px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.20); color: rgba(255,255,255,0.92); }}
      input[type="color"] {{ width: 46px; height: 34px; padding: 0; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.20); }}
      select {{ width: 100%; padding: 8px 10px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.20); color: rgba(255,255,255,0.92); }}
      .preview {{ display:flex; align-items:center; gap: 10px; }}
      .ico {{ width: 28px; height: 28px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.03); display:inline-flex; align-items:center; justify-content:center; }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 10; }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="top">
      <div class="bar">
        <div>
          <div style="font-size:18px;">Tag: icone & colori</div>
          <div class="hint">Definisci qui i tag disponibili (poi compariranno come menu a tendina in index_debug per le uscite).</div>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
          <a class="btn" href="/index_debug">← index_debug</a>
          <button class="btn" id="addRow">Aggiungi</button>
          <button class="btn" id="saveAll">Salva</button>
        </div>
      </div>
    </div>
    <div class="wrap">
      <table>
        <thead>
          <tr>
            <th style="width: 18%;">Nome tag</th>
            <th style="width: 18%;">Icona OFF</th>
            <th style="width: 18%;">Icona ON</th>
            <th style="width: 10%;">Colore OFF</th>
            <th style="width: 10%;">Colore ON</th>
            <th>Preview</th>
            <th style="width: 8%;">Azioni</th>
          </tr>
        </thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="toast" id="toast"></div>

    <script>
      const INITIAL = {initial};
      const tbody = document.getElementById('tbody');
      const toastEl = document.getElementById('toast');
      function toast(msg, ms=2600) {{
        if (!toastEl) return;
        toastEl.textContent = String(msg || '');
        toastEl.style.display = 'block';
        setTimeout(() => {{ toastEl.style.display = 'none'; }}, Number(ms || 2600));
      }}

      function esc(s) {{
        return String(s||'').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c]||c));
      }}

      const ICONS = {{
        // Official MDI SVG paths (MaterialDesignIcons.com / Templarian MaterialDesign repo).
        mdiGate: '<path d="M9 6V11H7V7H5V11H3V9H1V21H3V19H5V21H7V19H9V21H11V19H13V21H15V19H17V21H19V19H21V21H23V9H21V11H19V7H17V11H15V6H13V11H11V6H9M3 13H5V17H3V13M7 13H9V17H7V13M11 13H13V17H11V13M15 13H17V17H15V13M19 13H21V17H19V13Z" />',
        mdiBoomGate: '<path d="M20,9H8.22C7.11,7.77 5.21,7.68 4,8.8C3.36,9.36 3,10.16 3,11V20A1,1 0 0,0 2,21V22H10V21A1,1 0 0,0 9,20V13H20A2,2 0 0,0 22,11A2,2 0 0,0 20,9M6,12.5A1.5,1.5 0 0,1 4.5,11A1.5,1.5 0 0,1 6,9.5A1.5,1.5 0 0,1 7.5,11A1.5,1.5 0 0,1 6,12.5M10.5,12L9,10H10.5L12,12H10.5M14.5,12L13,10H14.5L16,12H14.5M18.5,12L17,10H18.5L20,12H18.5Z" />',
        mdiGarageVariant: '<path d="M22 9V20H20V11H4V20H2V9L12 5L22 9M19 12H5V14H19V12M19 18H5V20H19V18M19 15H5V17H19V15Z" />',
        mdiGarageOpenVariant: '<path d="M22 9V20H20V11H4V20H2V9L12 5L22 9M19 12H5V14H19V12Z" />',
        mdiGridLarge: '<path d="M4,2H20A2,2 0 0,1 22,4V20A2,2 0 0,1 20,22H4C2.92,22 2,21.1 2,20V4A2,2 0 0,1 4,2M4,4V11H11V4H4M4,20H11V13H4V20M20,20V13H13V20H20M20,4H13V11H20V4Z" />',
        mdiCurtainsClosed: '<path d="M23 3H1V1H23V3M2 22H11V4H2V22M22 4H13V22H22V4Z" />',
        mdiCurtains: '<path d="M23 3H1V1H23V3M2 22H6C6 19 4 17 4 17C10 13 11 4 11 4H2V22M22 4H13C13 4 14 13 20 17C20 17 18 19 18 22H22V4Z" />',
        mdiLightbulb: '<path d="M12,2A7,7 0 0,0 5,9C5,11.38 6.19,13.47 8,14.74V17A1,1 0 0,0 9,18H15A1,1 0 0,0 16,17V14.74C17.81,13.47 19,11.38 19,9A7,7 0 0,0 12,2M9,21A1,1 0 0,0 10,22H14A1,1 0 0,0 15,21V20H9V21Z" />',
        mdiBlindsHorizontalClosed: '<path d="M20 19V3H4V19H2V21H13.25C13.25 21.97 14.03 22.75 15 22.75S16.75 21.97 16.75 21H22V19H20M18 11H16V9H18V11M14 11H6V9H14V11M14 13V15H6V13H14M16 13H18V15H16V13M18 7H16V5H18V7M14 5V7H6V5H14M6 19V17H14V19H6M16 19V17H18V19H16Z" />',
        mdiBlindsHorizontal: '<path d="M20 19V3H4V19H2V21H22V19H20M16 9H18V11H16V9M14 11H6V9H14V11M18 7H16V5H18V7M14 5V7H6V5H14M6 19V13H14V14.82C13.55 15.14 13.25 15.66 13.25 16.25C13.25 17.22 14.03 18 15 18S16.75 17.22 16.75 16.25C16.75 15.66 16.45 15.13 16 14.82V13H18V19H6Z" />',
        mdiRollerShadeClosed: '<path d="M20 19V3H4V19H2V21H10.25C10.25 21.97 11.03 22.75 12 22.75S13.75 21.97 13.75 21H22V19H20M6 19V17H11V19H6M13 19V17H18V19H13Z" />',
        mdiRollerShade: '<path d="M20 19V3H4V19H2V21H22V19H20M6 19V13H11V14.8C10.6 15.1 10.2 15.6 10.2 16.2C10.2 17.2 11 18 12 18S13.8 17.2 13.8 16.2C13.8 15.6 13.5 15.1 13 14.8V13H18V19H6Z" />',
        mdiWindowShutter: '<path d="M3 4H21V8H19V20H17V8H7V20H5V8H3V4M8 9H16V11H8V9M8 12H16V14H8V12M8 15H16V17H8V15M8 18H16V20H8V18Z" />',
        mdiWindowShutterOpen: '<path d="M3 4H21V8H19V20H17V8H7V20H5V8H3V4M8 9H16V11H8V9Z" />',
        mdiPump: '<path d="M2 21V15H3.5C3.18 14.06 3 13.05 3 12C3 7.03 7.03 3 12 3H22V9H20.5C20.82 9.94 21 10.95 21 12C21 16.97 16.97 21 12 21H2M5 12C5 13.28 5.34 14.47 5.94 15.5L9.4 13.5C9.15 13.06 9 12.55 9 12C9 11.35 9.21 10.75 9.56 10.26L6.3 7.93C5.5 9.08 5 10.5 5 12M12 19C14.59 19 16.85 17.59 18.06 15.5L14.6 13.5C14.08 14.4 13.11 15 12 15L11.71 15L11.33 18.97L12 19M12 9C13.21 9 14.26 9.72 14.73 10.76L18.37 9.1C17.27 6.68 14.83 5 12 5V9M12 11C11.45 11 11 11.45 11 12C11 12.55 11.45 13 12 13C12.55 13 13 12.55 13 12C13 11.45 12.55 11 12 11Z" />',
      }};

      const ICON_KEYS = Object.keys(ICONS);
      function iconSelectHtml(value) {{
        const v = String(value || '');
        return ICON_KEYS.map(k => `<option value="${{esc(k)}}" ${{k===v?'selected':''}}>${{esc(k)}}</option>`).join('');
      }}
      function svgFor(key) {{
        const k = String(key || '');
        return ICONS[k] || ICONS.mdiGridLarge;
      }}
      function normalizeColor(v, fallback) {{
        const s = String(v || '').trim();
        if (!s) return fallback;
        return s;
      }}

      function rowTemplate(tag, st) {{
        const s = (st && typeof st === 'object') ? st : {{}};
        const iconOff = String(s.icon_off || 'mdiGridLarge');
        const iconOn = String(s.icon_on || iconOff || 'mdiGridLarge');
        const colOff = normalizeColor(s.color_off, '#a9b1c3');
        const colOn = normalizeColor(s.color_on, '#1ed760');
        const svgOff = String(s.svg_off || '');
        const svgOn = String(s.svg_on || '');
        return `
          <tr data-tag="${{esc(tag)}}">
            <td><input type="text" class="tagName" value="${{esc(tag)}}" placeholder="Es. Luci"/></td>
            <td><select class="iconOff">${{iconSelectHtml(iconOff)}}</select></td>
            <td><select class="iconOn">${{iconSelectHtml(iconOn)}}</select></td>
            <td><input type="color" class="colorOff" value="${{esc(colOff)}}" /></td>
            <td><input type="color" class="colorOn" value="${{esc(colOn)}}" /></td>
            <td>
              <div class="preview">
                <span class="ico" data-prev="off"><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${{svgOff || svgFor(iconOff)}}</svg></span>
                <span class="ico" data-prev="on"><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${{svgOn || svgFor(iconOn)}}</svg></span>
                <span class="hint">OFF/ON</span>
              </div>
              <input type="hidden" class="svgOff" value="${{esc(svgOff)}}" />
              <input type="hidden" class="svgOn" value="${{esc(svgOn)}}" />
            </td>
            <td style="display:flex; gap:8px; flex-wrap:wrap;">
              <button class="btn svgEdit" type="button">SVG</button>
              <button class="btn del" type="button">Elimina</button>
            </td>
          </tr>
        `;
      }}

      function renderAll() {{
        const keys = Object.keys(INITIAL || {{}}).sort((a,b) => a.localeCompare(b,'it',{{sensitivity:'base'}}));
        tbody.innerHTML = keys.map(k => rowTemplate(k, INITIAL[k])).join('') || '<tr><td colspan="7" class="hint">Nessun tag definito. Premi "Aggiungi".</td></tr>';
        bind();
        refreshPreviews();
      }}

      function collect() {{
        const out = {{}};
        for (const tr of tbody.querySelectorAll('tr[data-tag]')) {{
          const name = tr.querySelector('input.tagName')?.value || '';
          const tag = String(name || '').trim();
          if (!tag) continue;
          const icon_off = String(tr.querySelector('select.iconOff')?.value || '').trim();
          const icon_on = String(tr.querySelector('select.iconOn')?.value || '').trim();
          const color_off = String(tr.querySelector('input.colorOff')?.value || '').trim();
          const color_on = String(tr.querySelector('input.colorOn')?.value || '').trim();
          const svg_off = String(tr.querySelector('input.svgOff')?.value || '').trim();
          const svg_on = String(tr.querySelector('input.svgOn')?.value || '').trim();
          const st = {{ icon_off, icon_on, color_off, color_on }};
          if (svg_off) st.svg_off = svg_off;
          if (svg_on) st.svg_on = svg_on;
          out[tag] = st;
        }}
        return out;
      }}

      async function sendCmd(payload) {{
        const res = await fetch('../api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const txt = await res.text();
        let data = null;
        try {{ data = JSON.parse(txt); }} catch (_e) {{}}
        if (!res.ok || (data && data.ok === false)) {{
          throw new Error((data && data.error) ? data.error : txt);
        }}
        return data || {{}};
      }}

      async function saveAll() {{
        const next = collect();
        // Delete removed keys.
        const curKeys = new Set(Object.keys(INITIAL || {{}}));
        const nextKeys = new Set(Object.keys(next || {{}}));
        for (const k of curKeys) {{
          if (!nextKeys.has(k)) {{
            try {{ await sendCmd({{ type: 'tag_styles', action: 'delete', value: {{ tag: k }} }}); }} catch (_e) {{}}
          }}
        }}
        // Upsert current.
        for (const [tag, st] of Object.entries(next)) {{
          await sendCmd({{ type: 'tag_styles', action: 'set', value: {{ tag, ...st }} }});
        }}
        toast('Salvato');
        // Refresh page state.
        INITIAL && Object.keys(INITIAL).forEach(k => delete INITIAL[k]);
        Object.assign(INITIAL, next);
        renderAll();
      }}

      function refreshPreviews() {{
        for (const tr of tbody.querySelectorAll('tr')) {{
          const iconOff = String(tr.querySelector('select.iconOff')?.value || 'mdiGridLarge');
          const iconOn = String(tr.querySelector('select.iconOn')?.value || iconOff || 'mdiGridLarge');
          const colOff = String(tr.querySelector('input.colorOff')?.value || '#a9b1c3');
          const colOn = String(tr.querySelector('input.colorOn')?.value || '#1ed760');
          const svgOff = String(tr.querySelector('input.svgOff')?.value || '').trim();
          const svgOn = String(tr.querySelector('input.svgOn')?.value || '').trim();
          const off = tr.querySelector('[data-prev="off"]');
          const on = tr.querySelector('[data-prev="on"]');
          if (off) {{
            off.style.color = colOff;
            const svg = off.querySelector('svg');
            if (svg) svg.innerHTML = svgOff || svgFor(iconOff);
          }}
          if (on) {{
            on.style.color = colOn;
            const svg = on.querySelector('svg');
            if (svg) svg.innerHTML = svgOn || svgFor(iconOn);
          }}
        }}
      }}

      function bind() {{
        for (const tr of tbody.querySelectorAll('tr')) {{
          const del = tr.querySelector('button.del');
          if (del && !del.dataset.bound) {{
            del.dataset.bound = '1';
            del.addEventListener('click', (ev) => {{
              ev.preventDefault();
              tr.remove();
            }});
          }}
          const svgBtn = tr.querySelector('button.svgEdit');
          if (svgBtn && !svgBtn.dataset.bound) {{
            svgBtn.dataset.bound = '1';
            svgBtn.addEventListener('click', (ev) => {{
              ev.preventDefault();
              const curOff = String(tr.querySelector('input.svgOff')?.value || '');
              const curOn = String(tr.querySelector('input.svgOn')?.value || '');
              const nextOff = prompt('SVG OFF (incolla inner SVG, es. <path d=\"...\" />). Lascia vuoto per usare Icona OFF:', curOff);
              if (nextOff === null) return;
              const nextOn = prompt('SVG ON (incolla inner SVG). Lascia vuoto per usare Icona ON:', curOn);
              if (nextOn === null) return;
              const offEl = tr.querySelector('input.svgOff');
              const onEl = tr.querySelector('input.svgOn');
              if (offEl) offEl.value = String(nextOff || '').trim();
              if (onEl) onEl.value = String(nextOn || '').trim();
              refreshPreviews();
            }});
          }}
          for (const el of tr.querySelectorAll('input,select')) {{
            if (el.dataset.bound) continue;
            el.dataset.bound = '1';
            el.addEventListener('change', () => refreshPreviews());
            el.addEventListener('input', () => refreshPreviews());
          }}
        }}
      }}

      document.getElementById('addRow').addEventListener('click', (ev) => {{
        ev.preventDefault();
        const tag = prompt('Nome tag (es. Luci):');
        if (!tag) return;
        const t = String(tag).trim();
        if (!t) return;
        if (tbody.querySelector(`tr[data-tag="${{esc(t)}}"]`)) return;
        const html = rowTemplate(t, {{}});
        if (tbody.querySelector('tr td[colspan]')) tbody.innerHTML = '';
        tbody.insertAdjacentHTML('beforeend', html);
        bind();
        refreshPreviews();
      }});
      document.getElementById('saveAll').addEventListener('click', async (ev) => {{
        ev.preventDefault();
        try {{
          await saveAll();
        }} catch (e) {{
          toast('Errore: ' + String(e && e.message ? e.message : e), 3500);
        }}
      }});

      renderAll();
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_thermostat_detail(snapshot, thermostat_id: str):
    title = f"Termostato {thermostat_id}"
    for e in (snapshot.get("entities") or []):
        if str(e.get("type") or "").lower() == "thermostats" and str(e.get("id")) == str(thermostat_id):
            title = e.get("name") or title
            break

    init = json.dumps(snapshot, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - {_html_escape(title)}</title>
    <style>
      :root {{
        --bg: #0f1115;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: #2a2f3a;
        --card: #151923;
        --hover: #171a21;
        --btn: #1d2230;
        --btn2: #24304a;
        --gray: rgba(180,190,210,0.25);
        --gray2: rgba(180,190,210,0.12);
        --heat: rgba(255, 160, 60, 0.90);
        --heat2: rgba(255, 160, 60, 0.20);
        --cool: rgba(60, 160, 255, 0.92);
        --cool2: rgba(60, 160, 255, 0.20);
      }}
      body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; background: var(--bg); color: var(--fg); }}
      a {{ color: var(--fg); }}
      .wrap {{ max-width: 1220px; margin: 18px auto; padding: 0 16px; }}
      .top {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; }}
      .badge {{ display:inline-block; padding:2px 10px; border:1px solid var(--border); border-radius: 999px; color: var(--muted); }}
      .muted {{ color: var(--muted); }}
      .grid {{ display:grid; grid-template-columns: 1.2fr 1fr; gap: 12px; margin-top: 12px; }}
      .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }}
      .cardHead {{ padding: 12px 14px; background: rgba(255,255,255,0.03); border-bottom: 1px solid rgba(255,255,255,0.06); display:flex; align-items:center; justify-content:space-between; gap: 10px; }}
      .cardBody {{ padding: 12px 14px; }}
      .h {{ font-size: 14px; color: rgba(255,255,255,0.88); }}
      .row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
      .btn {{ background: var(--btn); border:1px solid var(--border); color: var(--fg); border-radius: 10px; padding: 7px 10px; cursor:pointer; }}
      .btn:hover {{ background: var(--btn2); }}
      select, input {{ background: #0f121a; color: var(--fg); border:1px solid var(--border); border-radius: 8px; padding: 6px 8px; }}
      input.rng {{ width: 220px; }}
      pre {{ margin:0; max-height: 260px; overflow:auto; white-space: pre; }}
      table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
      th, td {{ border-bottom: 1px solid var(--border); padding: 6px 6px; }}
      th {{ position: sticky; top: 0; background: rgba(0,0,0,0.25); }}
      tr:hover {{ background: var(--hover); }}
      td.h {{ width: 48px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
      td.cell select {{ width: 64px; }}
      .bg {{ position:fixed; inset:0; background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55)); filter: blur(28px); opacity: 0.85; pointer-events:none; }}
      .topbar {{ position:sticky; top:0; left:0; right:0; display:flex; align-items:center; justify-content:center; gap:18px; height:72px; background: rgba(0,0,0,0.55); backdrop-filter: blur(10px); z-index: 2; border-bottom: 1px solid rgba(255,255,255,0.06); }}
      .back {{ position:absolute; left:10px; top:50%; transform:translateY(-50%); display:inline-flex; align-items:center; justify-content:center; width:44px; height:44px; border-radius:999px; border:1px solid rgba(255,255,255,0.10); background: rgba(0,0,0,0.20); color: rgba(255,255,255,0.88); text-decoration:none; }}
      .tab {{ font-size:16px; letter-spacing:0.2px; color: rgba(255,255,255,0.70); text-decoration:none; padding:10px 12px; border-radius:12px; }}
      .tab.active {{ color:#fff; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 14px 16px 72px; }}
      .titleRow {{ display:flex; align-items:flex-start; justify-content:space-between; gap: 14px; margin-top: 8px; }}
      .chips {{ display:flex; gap: 10px; align-items:center; justify-content:flex-end; flex-wrap: wrap; }}
      .chip {{ display:inline-flex; align-items:center; gap:8px; padding: 8px 10px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.10); background: rgba(0,0,0,0.16); color: rgba(255,255,255,0.86); font-size: 13px; }}
      .chip .m {{ color: rgba(255,255,255,0.62); }}
      .panel {{ margin-top: 12px; border: 1px solid rgba(255,255,255,0.08); border-radius: 18px; background: rgba(255,255,255,0.02); overflow: hidden; }}
      .panelBody {{ padding: 16px 14px 18px; }}
      .hero {{ display:flex; align-items:center; justify-content:center; padding: 16px 10px 0; }}
      .ringWrap {{ position: relative; width: min(520px, 92vw); height: min(520px, 92vw); }}
      .ringSvg {{ position:absolute; inset:0; transform: rotate(-90deg); }}
      .center {{ position:absolute; inset: 12%; border-radius: 999px; background: radial-gradient(200px 200px at 40% 35%, rgba(255,255,255,0.14), rgba(0,0,0,0.42)); border: 1px solid rgba(255,255,255,0.08); box-shadow: 0 20px 60px rgba(0,0,0,0.55); display:flex; flex-direction:column; align-items:center; justify-content:center; gap: 10px; text-align:center; user-select: none; }}
      .big {{ font-size: clamp(64px, 10vw, 108px); font-weight: 300; letter-spacing: 0.5px; }}
      .tiny {{ font-size: 12px; color: rgba(255,255,255,0.60); }}
      .smallLine {{ display:flex; gap: 12px; align-items:center; justify-content:center; flex-wrap: wrap; color: rgba(255,255,255,0.72); font-size: 14px; }}
      .btnRow {{ display:flex; gap: 14px; align-items:center; justify-content:center; margin-top: 12px; }}
      .roundBtn {{ width: 52px; height: 52px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.12); background: rgba(0,0,0,0.16); color: rgba(255,255,255,0.92); cursor: pointer; display:flex; align-items:center; justify-content:center; }}
      .roundBtn:hover {{ background: rgba(255,255,255,0.06); }}
      .actionRow {{ display:flex; align-items:flex-end; justify-content:center; gap: 54px; flex-wrap: wrap; padding: 18px 8px 0; }}
      .action {{ width: 84px; display:flex; flex-direction:column; align-items:center; gap: 8px; cursor: pointer; user-select: none; }}
      .action .ico {{ width: 52px; height: 52px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.12); background: rgba(0,0,0,0.18); display:flex; align-items:center; justify-content:center; color: rgba(255,255,255,0.86); }}
      .action .lab {{ font-size: 13px; color: rgba(255,255,255,0.70); text-align:center; }}
      .action:hover .ico {{ background: rgba(255,255,255,0.06); }}
      .grid2 {{ display:grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }}
      @media (min-width: 920px) {{ .grid2 {{ grid-template-columns: 1fr 1fr; }} }}
      .toast {{ position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; backdrop-filter: blur(10px); display:none; z-index: 30; min-width: 180px; text-align: center; }}
      dialog {{ border: 1px solid rgba(255,255,255,0.12); border-radius: 16px; background: rgba(15,18,26,0.96); color: rgba(255,255,255,0.92); padding: 0; }}
      dialog::backdrop {{ background: rgba(0,0,0,0.55); }}
      .dlgHead {{ padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,0.08); display:flex; align-items:center; justify-content:space-between; gap: 10px; }}
      .dlgTitle {{ font-size: 15px; }}
      .dlgList {{ padding: 8px; }}
      .dlgItem {{ display:flex; align-items:center; justify-content:space-between; gap: 10px; padding: 12px 12px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.03); cursor: pointer; margin: 8px; }}
      .dlgItem:hover {{ background: rgba(255,255,255,0.06); }}
      .k {{ font-size: 12px; color: rgba(255,255,255,0.62); }}
      .v {{ font-size: 14px; color: rgba(255,255,255,0.92); }}
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="../thermostats" title="Lista termostati">&larr;</a>
      <a class="tab" id="tabTemp" href="#temperature">Temperatura</a>
      <a class="tab" id="tabHum" href="#humidity">Umidit&agrave;</a>
      <a class="tab" id="tabSch" href="#schedule">Schedulazione</a>
      <a class="tab" id="tabEx" href="#extra">Extra</a>
    </div>
    <div class="wrap">
      <div class="titleRow">
        <div>
          <div style="font-size:22px; font-weight:600; letter-spacing:0.2px">{_html_escape(title)}</div>
          <div class="muted" style="margin-top:4px">
            ID {_html_escape(str(thermostat_id))} &bull; v{_html_escape(ADDON_VERSION)} &bull; Aggiornato: <span id="lastUpdate">-</span>
          </div>
        </div>
        <div class="chips">
          <div class="chip"><span class="m">Stato</span> <span id="chipState">-</span></div>
          <div class="chip"><span class="m">Stagione</span> <span id="chipSeason">-</span></div>
          <div class="chip"><span class="m">Preset</span> <span id="chipMode">-</span></div>
          <div class="chip"><span class="m">Umidit&agrave;</span> <span id="chipRh">-</span></div>
        </div>
      </div>

      <div class="panel">
        <div class="panelBody" id="pageTemp">
          <div class="hero">
            <div class="ringWrap">
              <svg class="ringSvg" viewBox="0 0 200 200" aria-hidden="true">
                <circle id="ringBg" cx="100" cy="100" r="84" fill="none" stroke="var(--gray2)" stroke-width="10" stroke-linecap="round"></circle>
                <circle id="ringFg" cx="100" cy="100" r="84" fill="none" stroke="var(--gray)" stroke-width="10" stroke-linecap="round" stroke-dasharray="1 999"></circle>
              </svg>
              <div class="center">
                <div class="big" id="centerTemp">--,-</div>
                <div class="smallLine" id="centerSub">-</div>
                <div class="tiny" id="centerTarget">Set --,-</div>
                <div class="tiny" id="centerOut">Uscita: --</div>
              </div>
            </div>
          </div>
          <div class="btnRow">
            <button class="roundBtn" id="btnDown" type="button" title="-0.5">&minus;</button>
            <button class="roundBtn" id="btnUp" type="button" title="+0.5">+</button>
          </div>
          <div class="actionRow">
            <div class="action" id="actSeason" role="button" tabindex="0">
              <div class="ico" aria-hidden="true">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 2c1 3-2 4-2 7s2 4 2 7c0 0 6-1 6-7 0-5-4-6-6-7z"></path>
                  <path d="M8 13c0 2 2 3 4 3s4-1 4-3"></path>
                </svg>
              </div>
              <div class="lab">Stagione</div>
            </div>
            <div class="action" id="actMode" role="button" tabindex="0">
              <div class="ico" aria-hidden="true">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 20v-6"></path>
                  <path d="M12 4v2"></path>
                  <path d="M6 20v-3"></path>
                  <path d="M6 4v8"></path>
                  <path d="M18 20v-8"></path>
                  <path d="M18 4v4"></path>
                  <circle cx="12" cy="12" r="2"></circle>
                  <circle cx="6" cy="14" r="2"></circle>
                  <circle cx="18" cy="10" r="2"></circle>
                </svg>
              </div>
              <div class="lab">Preset</div>
            </div>
            <div class="action" id="actSchedule" role="button" tabindex="0">
              <div class="ico" aria-hidden="true">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="3" y="4" width="18" height="18" rx="3"></rect>
                  <path d="M16 2v4"></path>
                  <path d="M8 2v4"></path>
                  <path d="M3 10h18"></path>
                </svg>
              </div>
              <div class="lab">Schedulazione</div>
            </div>
            <div class="action" id="actExtra" role="button" tabindex="0">
              <div class="ico" aria-hidden="true">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 20v-6"></path>
                  <path d="M12 4v2"></path>
                  <path d="M6 20v-3"></path>
                  <path d="M6 4v8"></path>
                  <path d="M18 20v-8"></path>
                  <path d="M18 4v4"></path>
                </svg>
              </div>
              <div class="lab">Extra</div>
            </div>
          </div>
        </div>
        <div class="panelBody" id="pageHum" style="display:none"></div>
        <div class="panelBody" id="pageSch" style="display:none"></div>
        <div class="panelBody" id="pageEx" style="display:none"></div>
      </div>

      <div class="top">
        <h2 style="margin:0">{_html_escape(title)} <span class="badge">ID {_html_escape(str(thermostat_id))}</span> <span class="badge">v{_html_escape(ADDON_VERSION)}</span></h2>
        <div class="muted"><a href="../thermostats">← Lista termostati</a> · <a href="../index_debug">index_debug</a></div>
      </div>
      <div id="status" class="muted" style="margin-top:8px">-</div>

      <div class="grid">
        <div class="card">
          <div class="row" id="controls"></div>
          <div style="margin-top:10px" class="muted">Programmazione oraria</div>
          <div class="row" style="margin-top:8px">
            <label class="muted">Stagione</label>
            <select id="seasonSel">
              <option value="WIN">WIN</option>
              <option value="SUM">SUM</option>
            </select>
            <label class="muted">Tabella</label>
            <select id="tableSel">
              <option value="MON">MON</option>
              <option value="TUE">TUE</option>
              <option value="WED">WED</option>
              <option value="THU">THU</option>
              <option value="FRI">FRI</option>
              <option value="SAT">SAT</option>
              <option value="SUN">SUN</option>
              <option value="SD1">SD1</option>
              <option value="SD2">SD2</option>
            </select>
            <button class="btn" id="reloadBtn">Ricarica</button>
          </div>
          <div style="margin-top:10px; overflow:auto; max-height: 520px">
            <table id="schedTbl">
              <thead><tr><th>h</th><th>Profilo</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <div class="muted">Realtime</div>
          <pre id="rt">-</pre>
          <div class="muted" style="margin-top:10px">Static</div>
          <pre id="st">-</pre>
        </div>
      </div>
    </div>
    <dialog id="picker">
      <div class="dlgHead">
        <div class="dlgTitle" id="pickerTitle">—</div>
        <button class="btn" id="pickerClose" type="button">Chiudi</button>
      </div>
      <div class="dlgList" id="pickerList"></div>
    </dialog>
    <div class="toast" id="toast"></div>
    <script>
      const TH_ID = String({json.dumps(str(thermostat_id))});
      let snap = {init};
      let sse = null;

      function apiRoot() {{
        const p = String(window.location && window.location.pathname ? window.location.pathname : '');
        if (p.startsWith('/api/hassio_ingress/')) {{
          const parts = p.split('/').filter(Boolean);
          if (parts.length >= 3) return '/' + parts.slice(0, 3).join('/');
        }}
        return '';
      }}
      function apiUrl(path) {{
        const root = apiRoot();
        const p = String(path || '');
        if (p.startsWith('/')) return root + p;
        return root + '/' + p;
      }}

      function escHtml(s) {{
        return String(s)
          .replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;')
          .replaceAll('>', '&gt;')
          .replaceAll('\"', '&quot;')
          .replaceAll(\"'\", '&#39;');
      }}

      function setToast(msg) {{
        const el = document.getElementById('toast');
        if (!el) return;
        el.textContent = String(msg || '');
        el.style.display = 'block';
        clearTimeout(setToast._t);
        setToast._t = setTimeout(() => {{ el.style.display = 'none'; }}, 1400);
      }}

      function getThermEntity() {{
        const ents = (snap && snap.entities) ? snap.entities : [];
        for (const e of ents) {{
          if (String(e.type||'').toLowerCase() === 'thermostats' && String(e.id) === TH_ID) return e;
        }}
        return null;
      }}

      async function fetchSnap() {{
        try {{
          const res = await fetch(apiUrl('/api/entities'), {{cache:'no-store'}});
          if (!res.ok) return;
          snap = await res.json();
          render();
        }} catch (_e) {{}}
      }}

      async function sendCmd(action, value=null) {{
        const payload = {{ type: 'thermostats', id: Number(TH_ID), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const res = await fetch(apiUrl('/api/cmd'), {{
          method:'POST',
          headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify(payload),
        }});
        let data = null;
        try {{ data = await res.json(); }} catch (_e) {{ data = null; }}
        if (!res.ok || !data || data.ok !== true) {{
          const err = (data && data.error) ? data.error : (res.status + '');
          throw new Error(err);
        }}
        setToast('OK');
        setTimeout(fetchSnap, 350);
      }}

      function debouncedSetTarget(val) {{
        if (!window.__ksThermDeb) window.__ksThermDeb = null;
        if (window.__ksThermDeb) clearTimeout(window.__ksThermDeb);
        window.__ksThermDeb = setTimeout(() => {{
          sendCmd('set_target', val);
        }}, 350);
      }}

      function renderControls(e) {{
        const row = document.getElementById('controls');
        row.innerHTML = '';
        if (!e) {{
          row.innerHTML = '<span class=\"muted\">Termostato non trovato</span>';
          return;
        }}
        const rt = e.realtime || {{}};
        const therm = (rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
        const mode = therm ? String(therm.ACT_MODEL || therm.ACT_MODE || '') : '';
        const sea = therm ? String(therm.ACT_SEA || '') : '';
        const thr = therm && therm.TEMP_THR ? therm.TEMP_THR : null;
        const target = (thr && thr.VAL !== undefined) ? String(thr.VAL) : '';
        const temp = (rt.TEMP !== undefined) ? String(rt.TEMP) : '';
        const rh = (rt.RH !== undefined) ? String(rt.RH) : '';
        const st = e.static || {{}};
        const manHrs = st.MAN_HRS || '';
        const seaKey = (sea === 'SUM' || sea === 'WIN') ? sea : 'WIN';
        const seaCfg = (st && st[seaKey] && typeof st[seaKey] === 'object') ? st[seaKey] : null;
        const t1 = seaCfg ? (seaCfg.T1 ?? '') : '';
        const t2 = seaCfg ? (seaCfg.T2 ?? '') : '';
        const t3 = seaCfg ? (seaCfg.T3 ?? '') : '';

        row.insertAdjacentHTML('beforeend',
          `<span class=\"muted\">Temp</span><span>${{escHtml(temp || '-')}}&deg;C</span>
           <span class=\"muted\">Umidit&agrave;</span><span>${{escHtml(rh || '-')}}%</span>
           <span class=\"muted\">Modo</span>
           <select id=\"modeSel\"></select>
           <span class=\"muted\" id=\"manLbl\">MAN_HRS</span>
           <input id=\"manInp\" type=\"number\" step=\"0.5\" min=\"0\" max=\"72\" value=\"${{escHtml(manHrs)}}\" style=\"width:90px\"/>
           <span class=\"muted\">Stagione</span>
           <select onchange=\"sendCmd('set_season',this.value)\" id=\"seaSel\">
             <option value=\"WIN\">WIN</option>
             <option value=\"SUM\">SUM</option>
           </select>
           <span class=\"muted\">Set</span>
           <input id=\"setRng\" class=\"rng\" type=\"range\" min=\"5\" max=\"35\" step=\"0.5\" value=\"${{escHtml(target||'20')}}\"/>
           <span id=\"setVal\" class=\"muted\">${{escHtml(target||'')}}&deg;C</span>
           <span class=\"muted\">T1</span><input id=\"t1Inp\" type=\"number\" step=\"0.5\" style=\"width:70px\" value=\"${{escHtml(t1)}}\"/>
           <span class=\"muted\">T2</span><input id=\"t2Inp\" type=\"number\" step=\"0.5\" style=\"width:70px\" value=\"${{escHtml(t2)}}\"/>
           <span class=\"muted\">T3</span><input id=\"t3Inp\" type=\"number\" step=\"0.5\" style=\"width:70px\" value=\"${{escHtml(t3)}}\"/>`
        );
        const modeSel = document.getElementById('modeSel');
        const opts = ['OFF','MAN','MAN_TMR','WEEKLY','SD1','SD2'];
        for (const o of opts) {{
          const lab = (o === 'WEEKLY') ? 'AUTO' : o;
          modeSel.insertAdjacentHTML('beforeend', `<option value=\"${{o}}\">${{lab}}</option>`);
        }}
        modeSel.value = String(mode||'');
        document.getElementById('seaSel').value = sea || 'WIN';

        function syncMan() {{
          const v = String(modeSel.value || '').toUpperCase();
          const show = (v === 'MAN_TMR');
          const manLbl = document.getElementById('manLbl');
          const manInp = document.getElementById('manInp');
          if (manLbl) manLbl.style.display = show ? '' : 'none';
          if (manInp) manInp.style.display = show ? '' : 'none';
        }}
        syncMan();
        modeSel.onchange = () => {{
          const v = String(modeSel.value || '').toUpperCase();
          syncMan();
          if (v === 'MAN_TMR') return;
          sendCmd('set_mode', modeSel.value);
        }};

        const manInp = document.getElementById('manInp');
        if (manInp) manInp.onchange = () => sendCmd('set_manual_timer', manInp.value);

        const setRng = document.getElementById('setRng');
        const setVal = document.getElementById('setVal');
        if (setRng) {{
          setRng.oninput = () => {{
            if (setVal) setVal.textContent = String(setRng.value) + '°C';
            debouncedSetTarget(setRng.value);
          }};
        }}

        function setProfile(key, inp) {{
          const s = String(document.getElementById('seaSel').value || '').toUpperCase();
          sendCmd('set_profile', {{season: s, key: key, value: inp.value}});
        }}
        const t1Inp = document.getElementById('t1Inp');
        const t2Inp = document.getElementById('t2Inp');
        const t3Inp = document.getElementById('t3Inp');
        if (t1Inp) t1Inp.onchange = () => setProfile('T1', t1Inp);
        if (t2Inp) t2Inp.onchange = () => setProfile('T2', t2Inp);
        if (t3Inp) t3Inp.onchange = () => setProfile('T3', t3Inp);
      }}

      function renderSchedule(e) {{
        const tbody = document.querySelector('#schedTbl tbody');
        tbody.innerHTML = '';
        if (!e) return;
        const st = e.static || {{}};
        const season = document.getElementById('seasonSel').value;
        const dayKey = document.getElementById('tableSel').value;
        const sea = (season === 'SUM' || season === 'WIN') ? st[season] : null;
        const arr = sea && Array.isArray(sea[dayKey]) ? sea[dayKey] : null;
        for (let h=0; h<24; h++) {{
          const cur = (arr && arr[h] && typeof arr[h] === 'object') ? arr[h] : null;
          const t = cur ? String(cur.T || '') : '';
          const opt = `
            <select data-h=\"${{h}}\">
              <option value=\"1\" ${{t==='1'?'selected':''}}>T1</option>
              <option value=\"2\" ${{t==='2'?'selected':''}}>T2</option>
              <option value=\"3\" ${{t==='3'?'selected':''}}>T3</option>
            </select>`;
          const tr = document.createElement('tr');
          tr.innerHTML = `<td class=\"h\">${{h}}</td><td class=\"cell\">${{opt}}</td>`;
          tbody.appendChild(tr);
        }}
        tbody.querySelectorAll('select[data-h]').forEach(sel => {{
          sel.addEventListener('change', async () => {{
            const hour = Number(sel.dataset.h);
            const val = String(sel.value||'');
            await sendCmd('set_schedule', {{season: season, day: dayKey, hour: hour, t: val}});
          }});
        }});
      }}

      function render() {{
        const e = getThermEntity();
        renderControls(e);
        document.getElementById('rt').textContent = e && e.realtime ? JSON.stringify(e.realtime, null, 2) : '-';
        document.getElementById('st').textContent = e && e.static ? JSON.stringify(e.static, null, 2) : '-';
        renderSchedule(e);
      }}

      // --- Fancy thermostat UI (tabs + ring) ---
      let __uiInit = false;
      function initNewLayout() {{
        if (__uiInit) return;
        __uiInit = true;
        try {{
          const wrap = document.querySelector('.wrap');
          const legacyTop = wrap ? wrap.querySelector('.top') : null;
          if (legacyTop) legacyTop.style.display = 'none';
          const status = document.getElementById('status');
          if (status) status.style.display = 'none';

          // Move legacy schedule/debug cards into our tab pages.
          const grid = wrap ? wrap.querySelector('.grid') : null;
          const pageSch = document.getElementById('pageSch');
          const pageEx = document.getElementById('pageEx');
          if (grid && pageSch && pageEx) {{
            const cards = grid.querySelectorAll(':scope > .card');
            if (cards && cards[0]) pageSch.appendChild(cards[0]);
            if (cards && cards[1]) pageEx.appendChild(cards[1]);
            try {{ grid.remove(); }} catch (_e) {{}}
          }}
          const controls = document.getElementById('controls');
          if (controls) controls.style.display = 'none';

          const pageHum = document.getElementById('pageHum');
          if (pageHum) {{
            pageHum.innerHTML = `
              <div class="grid2">
                <div class="card">
                  <div class="cardHead"><div class="h">Umidità</div><div class="badge" id="humBadge">--%</div></div>
                  <div class="cardBody">
                    <div class="row">
                      <div><div class="k">RH</div><div class="v" id="humVal">--%</div></div>
                      <div class="badge" id="humTs">-</div>
                    </div>
                  </div>
                </div>
                <div class="card">
                  <div class="cardHead"><div class="h">Temperatura</div><div class="badge" id="tBadge">--</div></div>
                  <div class="cardBody">
                    <div class="row">
                      <div><div class="k">TEMP</div><div class="v" id="tVal">--</div></div>
                      <div class="badge" id="tTs">-</div>
                    </div>
                  </div>
                </div>
              </div>`;
          }}

          if (pageEx) {{
            const extra = document.createElement('div');
            extra.innerHTML = `
              <div class="grid2">
                <div class="card">
                  <div class="cardHead"><div class="h">Nome</div><button class="btn" id="btnRename" type="button">Salva</button></div>
                  <div class="cardBody">
                    <div class="row">
                      <div style="flex:1"><div class="k">Descrizione</div><div class="v">Visibile nella lista termostati</div></div>
                      <input id="nameInp" placeholder="Nome termostato..." style="flex:1; max-width: 320px"/>
                    </div>
                  </div>
                </div>
                <div class="card">
                  <div class="cardHead">
                    <div class="h">Profili (T1/T2/T3/TM)</div>
                    <div style="display:flex; gap:10px; align-items:center;">
                      <select id="profSeason"><option value="WIN">WIN</option><option value="SUM">SUM</option></select>
                      <button class="btn" id="btnProfSave" type="button">Salva</button>
                    </div>
                  </div>
                  <div class="cardBody">
                    <div class="row"><div><div class="k">T1</div><div class="v">Setpoint 1</div></div><input id="profT1" type="number" step="0.5" min="5" max="35" style="width:120px"/></div>
                    <div class="row"><div><div class="k">T2</div><div class="v">Setpoint 2</div></div><input id="profT2" type="number" step="0.5" min="5" max="35" style="width:120px"/></div>
                    <div class="row"><div><div class="k">T3</div><div class="v">Setpoint 3</div></div><input id="profT3" type="number" step="0.5" min="5" max="35" style="width:120px"/></div>
                    <div class="row"><div><div class="k">TM</div><div class="v">Manuale</div></div><input id="profTM" type="number" step="0.5" min="5" max="35" style="width:120px"/></div>
                  </div>
                </div>
              </div>`;
            pageEx.prepend(extra);
          }}
        }} catch (_e) {{}}
      }}

      function setTab(which) {{
        const map = {{
          temperature: ['pageTemp','tabTemp'],
          humidity: ['pageHum','tabHum'],
          schedule: ['pageSch','tabSch'],
          extra: ['pageEx','tabEx'],
        }};
        for (const k of Object.keys(map)) {{
          const ids = map[k];
          const page = document.getElementById(ids[0]);
          const tab = document.getElementById(ids[1]);
          const on = (k === which);
          if (page) page.style.display = on ? '' : 'none';
          if (tab) tab.classList.toggle('active', on);
        }}
      }}
      function syncHash() {{
        const h = String(window.location.hash || '').replace('#','').trim().toLowerCase();
        if (h === 'humidity' || h === 'schedule' || h === 'extra') setTab(h);
        else setTab('temperature');
      }}

      function modeLabel2(m) {{
        const x = String(m || '').toUpperCase();
        if (x === 'OFF') return 'Off';
        if (x === 'MAN') return 'Manuale';
        if (x === 'MAN_TMR') return 'Manuale (timer)';
        if (x === 'WEEKLY' || x === 'AUTO') return 'Auto';
        if (x === 'SD1') return 'SD1';
        if (x === 'SD2') return 'SD2';
        return x || '—';
      }}
      function seasonLabel2(s) {{
        const x = String(s || '').toUpperCase();
        return x === 'SUM' ? 'Estate' : 'Inverno';
      }}
      function fmtDec2(s) {{
        if (!s) return '';
        const n = Number(String(s).replace(',', '.'));
        if (!Number.isFinite(n)) return String(s);
        return n.toFixed(1).replace('.', ',');
      }}

      function ringSetColor2(outOn, season) {{
        const fg = document.getElementById('ringFg');
        const bg = document.getElementById('ringBg');
        let c = 'var(--gray)';
        let c2 = 'var(--gray2)';
        if (outOn) {{
          c = (season === 'SUM') ? 'var(--cool)' : 'var(--heat)';
          c2 = (season === 'SUM') ? 'var(--cool2)' : 'var(--heat2)';
        }}
        if (fg) fg.style.stroke = c;
        if (bg) bg.style.stroke = outOn ? c2 : 'var(--gray2)';
      }}
      function ringSetValue2(val) {{
        const fg = document.getElementById('ringFg');
        if (!fg) return;
        const r = 84;
        const C = 2 * Math.PI * r;
        let n = Number(String(val || '').replace(',', '.'));
        if (!Number.isFinite(n)) n = 20;
        n = Math.max(5, Math.min(35, n));
        const pct = (n - 5) / 30;
        const dash = Math.max(0.01, Math.min(0.999, pct)) * C;
        fg.setAttribute('stroke-dasharray', String(dash.toFixed(2)) + ' ' + String((C - dash).toFixed(2)));
      }}

      function getTarget2(therm, ent) {{
        const thr = therm && therm.TEMP_THR && typeof therm.TEMP_THR === 'object' ? therm.TEMP_THR : null;
        const v = thr && thr.VAL !== undefined && thr.VAL !== null ? String(thr.VAL) : '';
        if (v) return v;
        const st = ent && ent.static && typeof ent.static === 'object' ? ent.static : null;
        const sea = therm && therm.ACT_SEA ? String(therm.ACT_SEA).toUpperCase() : 'WIN';
        const seaCfg = st && st[sea] && typeof st[sea] === 'object' ? st[sea] : null;
        const tm = seaCfg && seaCfg.TM !== undefined && seaCfg.TM !== null ? String(seaCfg.TM) : '';
        return tm || '';
      }}

      function openPicker2(title, items, onPick) {{
        const dlg = document.getElementById('picker');
        const list = document.getElementById('pickerList');
        const head = document.getElementById('pickerTitle');
        if (!dlg || !list || !head) return;
        head.textContent = String(title || '');
        list.innerHTML = (items || []).map(it => {{
          return '<div class=\"dlgItem\" data-v=\"' + escHtml(it.value) + '\">' +
            '<div>' + escHtml(it.label) + '</div>' +
            (it.hint ? '<div class=\"badge\">' + escHtml(it.hint) + '</div>' : '<div></div>') +
          '</div>';
        }}).join('');
        list.querySelectorAll('.dlgItem[data-v]').forEach(el => {{
          el.onclick = () => {{
            const v = el.getAttribute('data-v');
            try {{ onPick(v); }} catch (_e) {{}}
            dlg.close();
          }};
        }});
        dlg.showModal();
      }}

      function wireAction2(id, fn) {{
        const el = document.getElementById(id);
        if (!el) return;
        el.onclick = fn;
        el.onkeydown = (ev) => {{
          if (ev.key === 'Enter' || ev.key === ' ') {{ ev.preventDefault(); fn(); }}
        }};
      }}

      function adjustTarget2(delta) {{
        const e = getThermEntity();
        if (!e) return;
        const rt = e.realtime || {{}};
        const therm = (rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
        const t = getTarget2(therm, e);
        let n = Number(String(t || '20').replace(',', '.'));
        if (!Number.isFinite(n)) n = 20;
        n = Math.max(5, Math.min(35, n + delta));
        sendCmd('set_target', n.toFixed(1)).catch(e => setToast('Errore: ' + (e && e.message ? e.message : e)));
      }}

      // Override: renderControls now updates the new UI + extra panels.
      function renderControls(e) {{
        initNewLayout();
        syncHash();

        const meta = snap && snap.meta && typeof snap.meta === 'object' ? snap.meta : null;
        const last = meta && meta.last_update ? Number(meta.last_update) : 0;
        const lastStr = last ? new Date(last * 1000).toISOString().replace('T',' ').slice(0,19) : '-';
        const lastEl = document.getElementById('lastUpdate');
        if (lastEl) lastEl.textContent = lastStr;

        const rtPre = document.getElementById('rt');
        const stPre = document.getElementById('st');
        if (rtPre) rtPre.style.whiteSpace = 'pre';
        if (stPre) stPre.style.whiteSpace = 'pre';

        if (!e) {{
          const sub = document.getElementById('centerSub');
          if (sub) sub.textContent = 'Termostato non trovato';
          return;
        }}

        const rt = e.realtime || {{}};
        const therm = (rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
        const mode = therm ? String(therm.ACT_MODEL || therm.ACT_MODE || '') : '';
        const sea = therm ? String(therm.ACT_SEA || '') : '';
        const out = therm ? String(therm.OUT_STATUS || '').toUpperCase() : '';
        const outOn = (out === 'ON'); // rispetta esattamente lo switch
        const season = (String(sea || 'WIN').toUpperCase() === 'SUM') ? 'SUM' : 'WIN';
        const temp = (rt.TEMP !== undefined && rt.TEMP !== null) ? String(rt.TEMP) : '';
        const rh = (rt.RH !== undefined && rt.RH !== null) ? String(rt.RH) : '';
        const target = getTarget2(therm, e);

        const chipSeason = document.getElementById('chipSeason');
        const chipMode = document.getElementById('chipMode');
        const chipRh = document.getElementById('chipRh');
        const chipState = document.getElementById('chipState');
        if (chipSeason) chipSeason.textContent = seasonLabel2(season);
        if (chipMode) chipMode.textContent = modeLabel2(mode);
        if (chipRh) chipRh.textContent = rh ? (String(rh) + '%') : '-';
        if (chipState) chipState.textContent = outOn ? (season === 'SUM' ? 'ON (blu)' : 'ON (caldo)') : 'OFF';

        ringSetColor2(outOn, season);
        ringSetValue2(target || '20');
        const sub = document.getElementById('centerSub');
        const ctemp = document.getElementById('centerTemp');
        const ctgt = document.getElementById('centerTarget');
        const cout = document.getElementById('centerOut');
        if (sub) sub.textContent = seasonLabel2(season) + ' · ' + modeLabel2(mode);
        if (ctemp) ctemp.textContent = fmtDec2(temp || '--') || '--,-';
        if (ctgt) ctgt.textContent = 'Set ' + (fmtDec2(target) || '--,-');
        if (cout) cout.textContent = 'Uscita: ' + (out || '--');

        const humVal = document.getElementById('humVal');
        const humBadge = document.getElementById('humBadge');
        const humTs = document.getElementById('humTs');
        if (humVal) humVal.textContent = rh ? (String(rh) + '%') : '--%';
        if (humBadge) humBadge.textContent = rh ? (String(rh) + '%') : '--%';
        if (humTs) humTs.textContent = lastStr;
        const tVal = document.getElementById('tVal');
        const tBadge = document.getElementById('tBadge');
        const tTs = document.getElementById('tTs');
        if (tVal) tVal.textContent = temp ? (fmtDec2(temp) + '°C') : '--';
        if (tBadge) tBadge.textContent = temp ? (fmtDec2(temp) + '°C') : '--';
        if (tTs) tTs.textContent = lastStr;

        // Extra: name + profiles
        const nameInp = document.getElementById('nameInp');
        if (nameInp && !nameInp._dirty) nameInp.value = String(e.name || '');
        const psEl = document.getElementById('profSeason');
        const ps = psEl ? String(psEl.value || season).toUpperCase() : season;
        const st = e.static || {{}};
        const cfg = st && st[ps] && typeof st[ps] === 'object' ? st[ps] : null;
        const setIfClean = (id, v) => {{
          const el = document.getElementById(id);
          if (!el) return;
          if (el._dirty) return;
          el.value = (v !== undefined && v !== null) ? String(v).replace(',', '.') : '';
        }};
        setIfClean('profT1', cfg ? cfg.T1 : '');
        setIfClean('profT2', cfg ? cfg.T2 : '');
        setIfClean('profT3', cfg ? cfg.T3 : '');
        setIfClean('profTM', cfg ? (cfg.TM ?? target) : target);
      }}

      function startSSE2() {{
        try {{ sse = new EventSource(apiUrl('/api/stream')); }} catch (_e) {{ sse = null; return; }}
        sse.onmessage = (ev) => {{
          let msg = null;
          try {{ msg = JSON.parse(ev.data || '{{}}'); }} catch (_e) {{ msg = null; }}
          if (!msg) return;
          const ents = Array.isArray(msg.entities) ? msg.entities : [];
          for (const e of ents) {{
            if (String(e.type||'').toLowerCase() === 'thermostats' && String(e.id) === TH_ID) {{
              const cur = getThermEntity();
              if (cur) {{
                cur.realtime = e.realtime;
                cur.static = e.static;
                cur.name = e.name;
              }}
              if (!snap.meta) snap.meta = {{}};
              if (msg.meta && msg.meta.last_update) snap.meta.last_update = msg.meta.last_update;
              render();
              return;
            }}
          }}
        }};
        sse.onerror = () => {{ try {{ if (sse) sse.close(); }} catch (_e) {{}}; }};
      }}

      function wireExtra2() {{
        const nameInp = document.getElementById('nameInp');
        if (nameInp) {{
          nameInp.addEventListener('input', () => {{ nameInp._dirty = true; }});
          nameInp.addEventListener('blur', () => {{ setTimeout(() => {{ nameInp._dirty = false; }}, 1200); }});
        }}
        for (const id of ['profT1','profT2','profT3','profTM']) {{
          const el = document.getElementById(id);
          if (!el) continue;
          el.addEventListener('input', () => {{ el._dirty = true; }});
          el.addEventListener('blur', () => {{ setTimeout(() => {{ el._dirty = false; }}, 1200); }});
        }}
        const btnRename = document.getElementById('btnRename');
        if (btnRename) btnRename.onclick = async () => {{
          const v = String((document.getElementById('nameInp')||{{}}).value || '').trim();
          if (!v) {{ setToast('Nome vuoto'); return; }}
          try {{ await sendCmd('set_description', v); }} catch (e) {{ setToast('Errore: ' + (e && e.message ? e.message : e)); }}
        }};
        const btnProfSave = document.getElementById('btnProfSave');
        if (btnProfSave) btnProfSave.onclick = async () => {{
          const s = String((document.getElementById('profSeason')||{{}}).value || 'WIN').toUpperCase();
          const t1 = String((document.getElementById('profT1')||{{}}).value || '').trim();
          const t2 = String((document.getElementById('profT2')||{{}}).value || '').trim();
          const t3 = String((document.getElementById('profT3')||{{}}).value || '').trim();
          const tm = String((document.getElementById('profTM')||{{}}).value || '').trim();
          const patch = {{}}; patch[s] = {{}};
          if (t1) patch[s].T1 = t1;
          if (t2) patch[s].T2 = t2;
          if (t3) patch[s].T3 = t3;
          if (tm) patch[s].TM = tm;
          try {{ await sendCmd('write_patch', patch); }} catch (e) {{ setToast('Errore: ' + (e && e.message ? e.message : e)); }}
        }};
        const ps = document.getElementById('profSeason');
        if (ps) ps.onchange = () => {{
          for (const id of ['profT1','profT2','profT3','profTM']) {{
            const el = document.getElementById(id);
            if (el) el._dirty = false;
          }}
          render();
        }};
      }}

      // Wire UI buttons
      document.getElementById('pickerClose').onclick = () => document.getElementById('picker').close();
      document.getElementById('btnDown').onclick = () => adjustTarget2(-0.5);
      document.getElementById('btnUp').onclick = () => adjustTarget2(+0.5);
      wireAction2('actSchedule', () => {{ window.location.hash = '#schedule'; syncHash(); }});
      wireAction2('actExtra', () => {{ window.location.hash = '#extra'; syncHash(); }});
      wireAction2('actSeason', () => {{
        const e = getThermEntity();
        const rt = e ? (e.realtime || {{}}) : {{}};
        const therm = (rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
        const cur = therm && therm.ACT_SEA ? String(therm.ACT_SEA).toUpperCase() : 'WIN';
        openPicker2('Stagione', [
          {{ value:'WIN', label:'Inverno (caldo)', hint: cur === 'WIN' ? 'attiva' : '' }},
          {{ value:'SUM', label:'Estate (freddo)', hint: cur === 'SUM' ? 'attiva' : '' }},
        ], (v) => {{ sendCmd('set_season', v).catch(e => setToast('Errore: ' + (e && e.message ? e.message : e))); }});
      }});
      wireAction2('actMode', () => {{
        const e = getThermEntity();
        const rt = e ? (e.realtime || {{}}) : {{}};
        const therm = (rt.THERM && typeof rt.THERM === 'object') ? rt.THERM : null;
        const cur = therm ? String(therm.ACT_MODEL || therm.ACT_MODE || '').toUpperCase() : '';
        const items = [
          {{ value:'OFF', label:'Off', hint: cur === 'OFF' ? 'attivo' : '' }},
          {{ value:'MAN', label:'Manuale', hint: cur === 'MAN' ? 'attivo' : '' }},
          {{ value:'MAN_TMR', label:'Manuale (timer)', hint: cur === 'MAN_TMR' ? 'attivo' : '' }},
          {{ value:'WEEKLY', label:'Auto (settimanale)', hint: (cur === 'WEEKLY' || cur === 'AUTO') ? 'attivo' : '' }},
          {{ value:'SD1', label:'SD1', hint: cur === 'SD1' ? 'attivo' : '' }},
          {{ value:'SD2', label:'SD2', hint: cur === 'SD2' ? 'attivo' : '' }},
        ];
        openPicker2('Preset / Modo', items, (v) => {{ sendCmd('set_mode', v).catch(e => setToast('Errore: ' + (e && e.message ? e.message : e))); }});
      }});
      window.addEventListener('hashchange', syncHash);
      initNewLayout();
      wireExtra2();
      syncHash();
      startSSE2();

      document.getElementById('reloadBtn').onclick = fetchSnap;
      document.getElementById('seasonSel').onchange = render;
      document.getElementById('tableSel').onchange = render;
      render();
    </script>
  </body>
</html>"""
    return html.encode("utf-8")


def render_logs(snapshot):
    entities = snapshot.get("entities") or []
    meta = snapshot.get("meta") or {}

    logs = []
    for e in entities:
        if str(e.get("type") or "").lower() != "logs":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        item = {**st, **rt}
        if "ID" not in item:
            item["ID"] = e.get("id")
        logs.append(item)

    def _id_desc(x):
        try:
            return -int(str(x.get("ID") or 0))
        except Exception:
            return 0

    logs.sort(key=_id_desc)
    logs = logs[:500]

    init_payload = _html_escape(json.dumps({"logs": logs}, ensure_ascii=False))

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Registro Eventi</title>
    <style>
      :root {{
        --bg: #0f1115;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: #2a2f3a;
        --hover: #171a21;
        --badge-bg: #1f2430;
        --th-bg: #0f1115;
        --input-bg: #0f1115;
        --input-fg: #e7eaf0;
      }}
      body {{
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        margin: 16px;
        background: var(--bg);
        color: var(--fg);
      }}
      a {{ color: var(--fg); text-decoration: none; }}
      .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 12px; }}
      .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background: var(--badge-bg); }}
      .toolbar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }}
      input {{
        padding: 8px;
        width: 420px;
        max-width: 100%;
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
      }}
      button {{
        background: var(--badge-bg);
        color: var(--fg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 6px 10px;
        cursor: pointer;
      }}
      button:hover {{ background: var(--hover); }}
      #wrap {{
        width: 100%;
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: 12px;
      }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid var(--border); padding: 10px 10px; vertical-align: top; }}
      th {{ text-align: left; position: sticky; top: 0; background: var(--th-bg); }}
      tr:hover {{ background: var(--hover); }}
      .small {{ font-size: 12px; color: var(--muted); }}
      .pager {{ display: flex; align-items: center; gap: 10px; }}
      select {{
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 6px 8px;
      }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    </style>
  </head>
  <body>
    <div style="position:sticky;top:0;left:0;right:0;z-index:5;display:flex;align-items:center;justify-content:center;gap:12px;height:64px;background:rgba(0,0,0,0.55);backdrop-filter:blur(10px);border-bottom:1px solid rgba(255,255,255,0.06);">
      <a href="/index_debug" style="color:#e8edf7;text-decoration:none;font-weight:700;">index_debug</a>
      <a href="/security" style="color:#e8edf7;text-decoration:none;font-weight:700;">sicurezza</a>
    </div>
    <div style="display:flex;align-items:center;justify-content:flex-start;margin:10px 0 0 0;">
      <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
    </div>
    <h2>Ksenia Lares - Registro Eventi {f'<span class="badge">v{_html_escape(ADDON_VERSION)}</span>' if ADDON_VERSION else ''}</h2>
    <div class="meta">
      Log: <span id="count" class="badge">{len(logs)}</span>
      &nbsp;|&nbsp; Last update: <span id="lastUpdate" class="badge">{_html_escape(_fmt_ts(meta.get("last_update")))}</span>
      &nbsp;|&nbsp; UI: <span class="badge">{_html_escape(UI_REV)}</span>
    </div>
    <div class="toolbar">
      <button id="toggle">Auto-refresh: ON</button>
      <input id="q" placeholder="Cerca (evento/info/tipo/data/ora)..." oninput="applyFilter()"/>
      <button onclick="exportJson()">Esporta JSON</button>
      <span class="pager">
        <span class="small">Per pagina</span>
        <select id="pageSize" onchange="setPageSize()">
          <option value="15">15</option>
          <option value="30">30</option>
          <option value="50">50</option>
          <option value="100">100</option>
        </select>
        <button onclick="prevPage()">‹</button>
        <span class="small">Pagina <span id="pageNo">1</span>/<span id="pageMax">1</span></span>
        <button onclick="nextPage()">›</button>
      </span>
    </div>
    <div id="wrap">
      <table>
        <thead>
          <tr>
            <th>Tipo</th>
            <th>Data</th>
            <th>Evento</th>
            <th>Info</th>
            <th>Immagine</th>
          </tr>
        </thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
    <script id="init" type="application/json">{init_payload}</script>
    <script>
      let pollingOn = true;
      let sse = null;
      let page = 1;
      let pageSize = 15;
      let filterQ = '';
      let logById = new Map();
      let ids = [];

      function esc(s) {{
        return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;').replaceAll(\"'\",'&#39;');
      }}

      function parseInit() {{
        const el = document.getElementById('init');
        if (!el) return;
        let payload = null;
        try {{ payload = JSON.parse(el.textContent || '{{}}'); }} catch (_e) {{ payload = null; }}
        const list = (payload && payload.logs) ? payload.logs : [];
        logById = new Map();
        ids = [];
        for (const it of list) {{
          if (!it || it.ID === undefined || it.ID === null) continue;
          const id = String(it.ID);
          logById.set(id, it);
          ids.push(id);
        }}
        ids.sort((a,b) => (parseInt(b,10)||0) - (parseInt(a,10)||0));
        document.getElementById('count').innerText = String(ids.length);
      }}

      function rowHtml(it) {{
        const typ = String(it.TYPE ?? '');
        const date = String(it.DATA ?? '');
        const time = String(it.TIME ?? '');
        const ev = String(it.EV ?? '');
        const i1 = String(it.I1 ?? '');
        const i2 = String(it.I2 ?? '');
        const iml = String(it.IML ?? '');
        const when = (date && time) ? (date + ' ' + time) : (date || time);
        const info = [i1, i2].filter(Boolean).join(' | ');
        const img = (iml === 'T') ? 'Sì' : (iml === 'F' ? 'No' : iml);
        return '<tr>' +
          '<td class="mono">' + esc(typ) + '</td>' +
          '<td class="mono">' + esc(when) + '</td>' +
          '<td>' + esc(ev) + '</td>' +
          '<td>' + esc(info) + '</td>' +
          '<td class="mono">' + esc(img) + '</td>' +
        '</tr>';
      }}

      function filteredIds() {{
        if (!filterQ) return ids.slice();
        const q = filterQ.toLowerCase();
        const out = [];
        for (const id of ids) {{
          const it = logById.get(id);
          if (!it) continue;
          const hay = (String(it.TYPE||'') + ' ' + String(it.DATA||'') + ' ' + String(it.TIME||'') + ' ' + String(it.EV||'') + ' ' + String(it.I1||'') + ' ' + String(it.I2||'')).toLowerCase();
          if (hay.includes(q)) out.push(id);
        }}
        return out;
      }}

      function render() {{
        const list = filteredIds();
        const maxPage = Math.max(1, Math.ceil(list.length / pageSize));
        if (page > maxPage) page = maxPage;
        if (page < 1) page = 1;
        document.getElementById('pageNo').innerText = String(page);
        document.getElementById('pageMax').innerText = String(maxPage);
        const start = (page - 1) * pageSize;
        const slice = list.slice(start, start + pageSize);
        const rows = [];
        for (const id of slice) {{
          const it = logById.get(id);
          if (it) rows.push(rowHtml(it));
        }}
        document.getElementById('tb').innerHTML = rows.join('');
      }}

      function applyFilter() {{
        filterQ = String(document.getElementById('q').value || '').trim();
        page = 1;
        render();
      }}

      function setPageSize() {{
        const v = parseInt(document.getElementById('pageSize').value || '15', 10);
        pageSize = isFinite(v) && v > 0 ? v : 15;
        page = 1;
        render();
      }}

      function prevPage() {{ page -= 1; render(); }}
      function nextPage() {{ page += 1; render(); }}

      function exportJson() {{
        const all = ids.map(id => logById.get(id)).filter(Boolean);
        const blob = new Blob([JSON.stringify(all, null, 2)], {{type: 'application/json'}});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'ksenia_logs.json';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      }}

      function connectSSE() {{
        if (sse) try {{ sse.close(); }} catch (_e) {{}}
        sse = new EventSource('/api/stream');
        sse.onmessage = (ev) => {{
          if (!pollingOn) return;
          let data = null;
          try {{ data = JSON.parse(ev.data); }} catch (_e) {{ return; }}
          const meta = data.meta || {{}};
          const lastUpdateStr = meta.last_update ? new Date(meta.last_update * 1000).toISOString().replace('T', ' ').slice(0, 19) : '-';
          const el = document.getElementById('lastUpdate');
          if (el) el.innerText = lastUpdateStr;
          const ents = data.entities || [];
          let changed = false;
          for (const e of ents) {{
            if (!e || String(e.type || '').toLowerCase() !== 'logs') continue;
            const id = String(e.id ?? '');
            if (!id) continue;
            const merged = Object.assign({{}}, e.static || {{}}, e.realtime || {{}});
            merged.ID = merged.ID ?? e.id;
            if (!logById.has(id)) {{
              ids.unshift(id);
              changed = true;
            }} else {{
              changed = true;
            }}
            logById.set(id, merged);
          }}
          if (changed) {{
            ids = Array.from(new Set(ids));
            ids.sort((a,b) => (parseInt(b,10)||0) - (parseInt(a,10)||0));
            document.getElementById('count').innerText = String(ids.length);
            render();
          }}
        }};
        sse.onerror = () => {{
          try {{ sse.close(); }} catch (_e) {{}}
          sse = null;
          setTimeout(() => connectSSE(), 1500);
        }};
      }}

      document.getElementById('toggle').onclick = () => {{
        pollingOn = !pollingOn;
        document.getElementById('toggle').innerText = 'Auto-refresh: ' + (pollingOn ? 'ON' : 'OFF');
      }};

      parseInit();
      render();
      connectSSE();
    </script>
  </body>
</html>
"""
    return html.encode("utf-8")


def render_timers(snapshot):
    entities = snapshot.get("entities") or []
    meta = snapshot.get("meta") or {}

    scenarios = {}
    for e in entities:
        if str(e.get("type") or "").lower() != "scenarios":
            continue
        try:
            sid = int(e.get("id"))
        except Exception:
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        name = st.get("DES") or st.get("NM") or e.get("name")
        if isinstance(name, str) and name.strip():
            scenarios[sid] = name.strip()

    timers = []
    for e in entities:
        if str(e.get("type") or "").lower() != "schedulers":
            continue
        st = e.get("static") if isinstance(e.get("static"), dict) else {}
        rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
        item = {**st, **rt}
        if "ID" not in item:
            item["ID"] = e.get("id")
        sce = item.get("SCE")
        try:
            if sce is not None:
                item["SCE_NAME"] = scenarios.get(int(str(sce))) or item.get("SCE_NAME")
        except Exception:
            pass
        timers.append(item)

    def _id_key(x):
        try:
            return int(str(x.get("ID") or 0))
        except Exception:
            return 0

    timers.sort(key=_id_key)
    init_payload = _html_escape(
        json.dumps(
            {"timers": timers, "scenarios": {str(k): v for k, v in scenarios.items()}},
            ensure_ascii=False,
        )
    )

    html = f"""<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Programmatori Orari</title>
    <style>
      :root {{
        --bg: #0f1115;
        --fg: #e7eaf0;
        --muted: #a9b1c3;
        --border: #2a2f3a;
        --hover: #171a21;
        --badge-bg: #1f2430;
        --th-bg: #0f1115;
        --input-bg: #0f1115;
        --input-fg: #e7eaf0;
        --ok: rgba(76, 175, 80, 0.12);
      }}
      body {{
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        margin: 16px;
        background: var(--bg);
        color: var(--fg);
      }}
      a {{ color: var(--fg); text-decoration: none; }}
      .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 12px; }}
      .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background: var(--badge-bg); }}
      .toolbar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }}
      input {{
        padding: 8px;
        width: 420px;
        max-width: 100%;
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
      }}
      button {{
        background: var(--badge-bg);
        color: var(--fg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 6px 10px;
        cursor: pointer;
      }}
      button:hover {{ background: var(--hover); }}
      #wrap {{
        width: 100%;
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: 12px;
      }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid var(--border); padding: 10px 10px; vertical-align: top; }}
      th {{ text-align: left; position: sticky; top: 0; background: var(--th-bg); }}
      tr:hover {{ background: var(--hover); }}
      tr.enabled {{ background: var(--ok); }}
      .small {{ font-size: 12px; color: var(--muted); }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
      .layout {{
        display: grid;
        grid-template-columns: 360px 1fr;
        gap: 12px;
        align-items: start;
      }}
      .panel {{
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 12px;
        background: rgba(255,255,255,0.02);
      }}
      .list-item {{
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 10px;
        margin-bottom: 10px;
        cursor: pointer;
      }}
      .list-item:hover {{ background: var(--hover); }}
      .list-item.active {{ outline: 2px solid rgba(33,150,243,0.5); }}
      .cards {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
      }}
      .card {{
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 12px;
        background: rgba(255,255,255,0.02);
        min-height: 120px;
      }}
      .card h3 {{
        margin: 0 0 10px 0;
        font-size: 14px;
      }}
      .row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 0;
        border-top: 1px dashed var(--border);
      }}
      .row:first-of-type {{ border-top: 0; padding-top: 0; }}
      .label {{ color: var(--muted); font-size: 12px; }}
      .value {{ font-size: 14px; }}
      .switch {{
        width: 42px;
        height: 22px;
      }}
      select {{
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 6px 8px;
        max-width: 100%;
      }}
      input[type="time"] {{
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 6px 8px;
      }}
      .day {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 0;
        border-top: 1px dashed var(--border);
      }}
      .day:first-of-type {{ border-top: 0; padding-top: 0; }}
    </style>
  </head>
  <body>
    <div style="position:sticky;top:0;left:0;right:0;z-index:5;display:flex;align-items:center;justify-content:center;gap:12px;height:64px;background:rgba(0,0,0,0.55);backdrop-filter:blur(10px);border-bottom:1px solid rgba(255,255,255,0.06);">
      <a href="/index_debug" style="color:#e8edf7;text-decoration:none;font-weight:700;">index_debug</a>
      <a href="/security" style="color:#e8edf7;text-decoration:none;font-weight:700;">sicurezza</a>
    </div>
    <div style="display:flex;align-items:center;justify-content:flex-start;margin:10px 0 0 0;">
      <img src="/assets/e-safe_scr.png" alt="e-safe" style="height:34px;opacity:0.92;pointer-events:none;"/>
    </div>
    <h2>Ksenia Lares - Programmatori Orari {f'<span class="badge">v{_html_escape(ADDON_VERSION)}</span>' if ADDON_VERSION else ''}</h2>
    <div class="meta">
      Programmatori: <span id="count" class="badge">{len(timers)}</span>
      &nbsp;|&nbsp; Last update: <span id="lastUpdate" class="badge">{_html_escape(_fmt_ts(meta.get("last_update")))}</span>
      &nbsp;|&nbsp; UI: <span class="badge">{_html_escape(UI_REV)}</span>
    </div>
    <div class="toolbar">
      <a class="badge" href="/index_debug">← index_debug</a>
      <button id="toggle">Auto-refresh: ON</button>
      <input id="q" placeholder="Cerca (descrizione/scenario/giorni/orario)..." oninput="applyFilter()"/>
    </div>
    <div class="layout">
      <div class="panel">
        <div class="small" style="margin-bottom:10px;">Elenco</div>
        <div id="list"></div>
      </div>
      <div class="panel">
        <div id="detail" class="cards"></div>
        <div id="status" class="small" style="margin-top:10px;"></div>
      </div>
    </div>
    <script id="init" type="application/json">{init_payload}</script>
    <script>
      let pollingOn = true;
      let sse = null;
      let filterQ = '';
      let byId = new Map();
      let ids = [];
      let selectedId = null;
      let scenarios = {{}};

      function esc(s) {{
        return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;').replaceAll(\"'\",'&#39;');
      }}

      function parseInit() {{
        const el = document.getElementById('init');
        let payload = null;
        try {{ payload = JSON.parse(el.textContent || '{{}}'); }} catch (_e) {{ payload = null; }}
        const list = (payload && payload.timers) ? payload.timers : [];
        scenarios = (payload && payload.scenarios) ? payload.scenarios : {{}};
        byId = new Map();
        ids = [];
        for (const it of list) {{
          if (!it || it.ID === undefined || it.ID === null) continue;
          const id = String(it.ID);
          byId.set(id, it);
          ids.push(id);
        }}
        ids.sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
        document.getElementById('count').innerText = String(ids.length);
      }}

      function daysStr(it) {{
        const map = [
          ['MON','Lun'], ['TUE','Mar'], ['WED','Mer'], ['THU','Gio'], ['FRI','Ven'], ['SAT','Sab'], ['SUN','Dom']
        ];
        const out = [];
        for (const pair of map) {{
          const k = pair[0], lab = pair[1];
          if (String(it[k] ?? '').toUpperCase() === 'T') out.push(lab);
        }}
        return out.join(', ');
      }}

      function timeStr(it) {{
        const h = it.H, m = it.M;
        if (h === undefined || m === undefined) return '';
        const hh = String(parseInt(h,10)).padStart(2,'0');
        const mm = String(parseInt(m,10)).padStart(2,'0');
        return hh + ':' + mm;
      }}

      async function sendCmd(type, id, action, value=null) {{
        const payload = {{ type: String(type), id: Number(id), action: String(action) }};
        if (value !== null && value !== undefined) payload.value = value;
        const status = document.getElementById('status');
        if (status) status.innerText = 'Invio...';
        const res = await fetch('./api/cmd', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        let txt = '';
        try {{ txt = await res.text(); }} catch (_e) {{ txt = ''; }}
        if (status) status.innerText = res.ok ? 'OK' : ('ERR: ' + txt);
        return res;
      }}

      function listHtml(it) {{
        const id = String(it.ID ?? '');
        const en = String(it.EN ?? '').toUpperCase() === 'T';
        const desc = String(it.DES ?? '');
        const when = timeStr(it);
        const days = daysStr(it);
        const sce = String(it.SCE_NAME ?? it.SCE ?? '');
        const active = (selectedId !== null && String(selectedId) === id) ? 'active' : '';
        const badge = en ? '<span class=\"badge\">ON</span>' : '<span class=\"badge\">OFF</span>';
        return '<div class=\"list-item ' + active + '\" data-id=\"' + esc(id) + '\">' +
          '<div style=\"display:flex; justify-content:space-between; gap:10px; align-items:center;\">' +
            '<div class=\"mono\">' + esc(id) + '</div>' +
            '<div>' + badge + '</div>' +
          '</div>' +
          '<div style=\"margin-top:6px; font-size:14px;\">' + esc(desc) + '</div>' +
          '<div class=\"small\" style=\"margin-top:4px;\">' + esc(days || '-') + '</div>' +
          '<div class=\"small\" style=\"margin-top:4px;\">' + esc(when || '-') + (sce ? (' · ' + esc(sce)) : '') + '</div>' +
        '</div>';
      }}

      function scenarioOptions(current) {{
        const opts = [];
        opts.push('<option value=\"0\">Nessuno</option>');
        const keys = Object.keys(scenarios || {{}}).sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
        for (const sid of keys) {{
          const name = scenarios[sid];
          const sel = String(current) === String(sid) ? ' selected' : '';
          opts.push('<option value=\"' + esc(sid) + '\"' + sel + '>' + esc(name) + '</option>');
        }}
        return opts.join('');
      }}

      function detailHtml(it) {{
        const id = String(it.ID ?? '');
        const en = String(it.EN ?? '').toUpperCase() === 'T';
        const desc = String(it.DES ?? '');
        const typ = String(it.TYPE ?? 'TIME');
        const when = timeStr(it);
        const sceId = String(it.SCE ?? '0');
        const excl = String(it.EXCL_HOLIDAYS ?? '').toUpperCase() === 'T';

        const daysMap = [
          ['MON','Lunedì'], ['TUE','Martedì'], ['WED','Mercoledì'], ['THU','Giovedì'],
          ['FRI','Venerdì'], ['SAT','Sabato'], ['SUN','Domenica']
        ];

        const gen = '<div class=\"card\">' +
          '<h3>Generali</h3>' +
          '<div class=\"row\">' +
            '<div><div class=\"label\">Abilitato</div></div>' +
            '<div><input class=\"switch\" type=\"checkbox\" ' + (en ? 'checked' : '') + ' data-id=\"' + esc(id) + '\" data-kind=\"en\"/></div>' +
          '</div>' +
          '<div class=\"row\">' +
            '<div style=\"flex:1; padding-right:10px;\"><div class=\"label\">Descrizione</div></div>' +
            '<div style=\"flex:2;\">' +
              '<input data-id=\"' + esc(id) + '\" data-kind=\"des\" value=\"' + esc(desc) + '\" style=\"width:100%; background: var(--input-bg); color: var(--input-fg); border:1px solid var(--border); border-radius:8px; padding:6px 8px;\"/>' +
            '</div>' +
          '</div>' +
          '<div class=\"row\">' +
            '<div style=\"flex:1; padding-right:10px;\"><div class=\"label\">Scenario</div></div>' +
            '<div style=\"flex:2;\">' +
              '<select data-id=\"' + esc(id) + '\" data-kind=\"sce\">' + scenarioOptions(sceId) + '</select>' +
            '</div>' +
          '</div>' +
        '</div>';

        const rep = '<div class=\"card\">' +
          '<h3>Ripetizioni</h3>' +
          daysMap.map(pair => {{
            const k = pair[0], label = pair[1];
            const on = String(it[k] ?? '').toUpperCase() === 'T';
            return '<div class=\"day\">' +
              '<div class=\"value\">' + esc(label) + '</div>' +
              '<div><input class=\"switch\" type=\"checkbox\" ' + (on ? 'checked' : '') + ' data-id=\"' + esc(id) + '\" data-kind=\"day\" data-day=\"' + esc(k) + '\"/></div>' +
            '</div>';
          }}).join('') +
        '</div>';

        const orario = '<div class=\"card\">' +
          '<h3>Orario</h3>' +
          '<div class=\"row\">' +
            '<div><div class=\"label\">Tipo</div><div class=\"value\">' + esc(typ) + '</div></div>' +
          '</div>' +
          '<div class=\"row\">' +
            '<div><div class=\"label\">Orario</div></div>' +
            '<div><input class=\"mono\" type=\"time\" value=\"' + esc(when) + '\" data-id=\"' + esc(id) + '\" data-kind=\"time\"/></div>' +
          '</div>' +
        '</div>';

        const fest = '<div class=\"card\">' +
          '<h3>Festivi</h3>' +
          '<div class=\"row\">' +
            '<div><div class=\"label\">Escludi festivi</div></div>' +
            '<div><input class=\"switch\" type=\"checkbox\" ' + (excl ? 'checked' : '') + ' data-id=\"' + esc(id) + '\" data-kind=\"hol\"/></div>' +
          '</div>' +
          '<div class=\"small\" style=\"margin-top:6px;\">Calendario festivi: vedi CFG_HOLIDAYS</div>' +
        '</div>';

        return gen + rep + orario + fest;
      }}

      function filteredIds() {{
        if (!filterQ) return ids.slice();
        const q = filterQ.toLowerCase();
        const out = [];
        for (const id of ids) {{
          const it = byId.get(id);
          if (!it) continue;
          const hay = (String(it.DES||'') + ' ' + String(it.SCE_NAME||it.SCE||'') + ' ' + timeStr(it) + ' ' + daysStr(it)).toLowerCase();
          if (hay.includes(q)) out.push(id);
        }}
        return out;
      }}

      function render() {{
        const list = filteredIds();
        if (selectedId === null && list.length) selectedId = list[0];
        const listRows = [];
        for (const id of list) {{
          const it = byId.get(id);
          if (it) listRows.push(listHtml(it));
        }}
        const listEl = document.getElementById('list');
        if (listEl) listEl.innerHTML = listRows.join('') || '<span class=\"small\">Nessun elemento</span>';

        const sel = selectedId !== null ? byId.get(String(selectedId)) : null;
        const detEl = document.getElementById('detail');
        if (detEl) detEl.innerHTML = sel ? detailHtml(sel) : '<span class=\"small\">Seleziona un programmatore</span>';

        const items = document.querySelectorAll('.list-item[data-id]');
        for (const el of items) {{
          el.onclick = () => {{
            selectedId = el.dataset.id;
            render();
          }};
        }}

        wireControls();
      }}

      function wireControls() {{
        const toggles = document.querySelectorAll('input[type=\"checkbox\"][data-kind=\"en\"][data-id]');
        for (const t of toggles) {{
          t.onchange = async () => {{
            const id = Number(t.dataset.id);
            const val = t.checked ? 'ON' : 'OFF';
            try {{
              await sendCmd('schedulers', id, 'set_enabled', val);
            }} catch (_e) {{}}
          }};
        }}
        const times = document.querySelectorAll('input[type=\"time\"][data-kind=\"time\"][data-id]');
        for (const ti of times) {{
          ti.onchange = async () => {{
            const id = Number(ti.dataset.id);
            const v = String(ti.value || '').trim();
            if (!v) return;
            try {{
              await sendCmd('schedulers', id, 'set_time', v);
            }} catch (_e) {{}}
          }};
        }}
        const sceSel = document.querySelectorAll('select[data-kind=\"sce\"][data-id]');
        for (const s of sceSel) {{
          s.onchange = async () => {{
            const id = Number(s.dataset.id);
            const v = Number(s.value || '0');
            try {{
              await sendCmd('schedulers', id, 'set_scenario', v);
            }} catch (_e) {{}}
          }};
        }}
        const descInputs = document.querySelectorAll('input[data-kind=\"des\"][data-id]');
        for (const inp of descInputs) {{
          inp.onchange = async () => {{
            const id = Number(inp.dataset.id);
            const v = String(inp.value || '').trim();
            if (!v) return;
            try {{
              await sendCmd('schedulers', id, 'set_description', v);
            }} catch (_e) {{}}
          }};
        }}
        const hol = document.querySelectorAll('input[type=\"checkbox\"][data-kind=\"hol\"][data-id]');
        for (const h of hol) {{
          h.onchange = async () => {{
            const id = Number(h.dataset.id);
            const val = h.checked ? 'ON' : 'OFF';
            try {{
              await sendCmd('schedulers', id, 'set_excl_holidays', val);
            }} catch (_e) {{}}
          }};
        }}
        const days = document.querySelectorAll('input[type=\"checkbox\"][data-kind=\"day\"][data-id][data-day]');
        for (const d of days) {{
          d.onchange = async () => {{
            const id = Number(d.dataset.id);
            const patch = {{}};
            const group = document.querySelectorAll('input[type=\"checkbox\"][data-kind=\"day\"][data-id=\"' + String(id) + '\"][data-day]');
            for (const x of group) {{
              patch[String(x.dataset.day)] = !!x.checked;
            }}
            try {{
              await sendCmd('schedulers', id, 'set_days', patch);
            }} catch (_e) {{}}
          }};
        }}
      }}

      function applyFilter() {{
        filterQ = String(document.getElementById('q').value || '').trim();
        render();
      }}

      function connectSSE() {{
        if (sse) try {{ sse.close(); }} catch (_e) {{}}
        sse = new EventSource('/api/stream');
        sse.onmessage = (ev) => {{
          if (!pollingOn) return;
          let data = null;
          try {{ data = JSON.parse(ev.data); }} catch (_e) {{ return; }}
          const meta = data.meta || {{}};
          const lastUpdateStr = meta.last_update ? new Date(meta.last_update * 1000).toISOString().replace('T', ' ').slice(0, 19) : '-';
          const el = document.getElementById('lastUpdate');
          if (el) el.innerText = lastUpdateStr;
          const ents = data.entities || [];
          let changed = false;
          for (const e of ents) {{
            if (!e || String(e.type || '').toLowerCase() !== 'schedulers') continue;
            const id = String(e.id ?? '');
            if (!id) continue;
            const merged = Object.assign({{}}, e.static || {{}}, e.realtime || {{}});
            merged.ID = merged.ID ?? e.id;
            byId.set(id, merged);
            if (!ids.includes(id)) ids.push(id);
            changed = true;
          }}
          // Keep scenarios map updated too (so scenario dropdown is populated).
          for (const e of ents) {{
            if (!e || String(e.type || '').toLowerCase() !== 'scenarios') continue;
            const sid = String(e.id ?? '');
            const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
            const nm = st.DES || st.NM || e.name || sid;
            if (sid) scenarios[sid] = String(nm || sid);
          }}
          if (changed) {{
            ids.sort((a,b) => (parseInt(a,10)||0) - (parseInt(b,10)||0));
            document.getElementById('count').innerText = String(ids.length);
            render();
          }}
        }};
        sse.onerror = () => {{
          try {{ sse.close(); }} catch (_e) {{}}
          sse = null;
          setTimeout(() => connectSSE(), 1500);
        }};
      }}

      async function refreshScenarios() {{
        try {{
          const res = await fetch('/api/entities');
          if (!res.ok) return;
          const snap = await res.json();
          const ents = snap.entities || [];
          const next = {{}};
          for (const e of ents) {{
            if (!e || String(e.type || '').toLowerCase() !== 'scenarios') continue;
            const sid = String(e.id ?? '');
            const st = (e.static && typeof e.static === 'object') ? e.static : {{}};
            const nm = st.DES || st.NM || e.name || sid;
            if (sid) next[sid] = String(nm || sid);
          }}
          if (Object.keys(next).length) {{
            scenarios = next;
            render();
          }}
        }} catch (_e) {{}}
      }}

      document.getElementById('toggle').onclick = () => {{
        pollingOn = !pollingOn;
        document.getElementById('toggle').innerText = 'Auto-refresh: ' + (pollingOn ? 'ON' : 'OFF');
      }};

      parseInit();
      render();
      refreshScenarios();
      connectSSE();
    </script>
  </body>
</html>
"""
    return html.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    state = None  # type: LaresState
    command_fn = None

    def _inject_ingress_shim(self, body: bytes) -> bytes:
        try:
            html = body.decode("utf-8", errors="ignore")
            if "</head>" not in html and "</body>" not in html:
                return body
            shim = r"""
<script>
  (function () {
    function ingressRoot() {
      try {
        var p = String(window.location && window.location.pathname ? window.location.pathname : "");
        if (p.indexOf("/api/hassio_ingress/") === 0) {
          var parts = p.split("/").filter(Boolean);
          if (parts.length >= 3) return "/" + parts.slice(0, 3).join("/");
        }
        var m = p.match(/^\/local_[^\/]+\/ingress/);
        if (m && m[0]) return m[0];
      } catch (e) {}
      return "";
    }
    var root = ingressRoot();
    if (!root) return;

    function prefix(url) {
      try {
        if (!url) return url;
        if (typeof url !== "string") return url;
        if (url[0] !== "/") return url;
        // Already ingress-prefixed? (avoid repeated prefixing by MutationObserver)
        if (url.indexOf(root + "/") === 0 || url === root) return url;
        if (url.indexOf("/api/hassio_ingress/") === 0) return url;
        if (/^\/local_[^\/]+\/ingress/.test(url)) return url;
        return root + url;
      } catch (e) {
        return url;
      }
    }

    function setAttrIfChanged(el, attr, value) {
      try {
        if (!el || !attr) return;
        var cur = el.getAttribute(attr);
        if (cur === value) return;
        el.setAttribute(attr, value);
      } catch (e) {}
    }

    var _rwTimer = null;
    function scheduleRewrite() {
      try {
        if (_rwTimer) return;
        _rwTimer = setTimeout(function () {
          _rwTimer = null;
          try { rewriteAll(); } catch (e) {}
        }, 0);
      } catch (e) {}
    }

    function rewriteAll() {
      try {
        document.querySelectorAll("a[href^='/']").forEach(function (a) {
          try { setAttrIfChanged(a, "href", prefix(a.getAttribute("href"))); } catch (e) {}
        });
      } catch (e) {}
      try {
        document.querySelectorAll("img[src^='/']").forEach(function (img) {
          try { setAttrIfChanged(img, "src", prefix(img.getAttribute("src"))); } catch (e) {}
        });
      } catch (e) {}
      try {
        document.querySelectorAll("link[href^='/']").forEach(function (lnk) {
          try { setAttrIfChanged(lnk, "href", prefix(lnk.getAttribute("href"))); } catch (e) {}
        });
      } catch (e) {}
      try {
        document.querySelectorAll("script[src^='/']").forEach(function (s) {
          try { setAttrIfChanged(s, "src", prefix(s.getAttribute("src"))); } catch (e) {}
        });
      } catch (e) {}
    }

    try { rewriteAll(); } catch (e) {}
    try {
      document.addEventListener("DOMContentLoaded", function () {
        try { rewriteAll(); } catch (e) {}
      });
    } catch (e) {}
    try {
      var mo = new MutationObserver(function () { try { scheduleRewrite(); } catch (e) {} });
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}

    try {
      var origFetch = window.fetch;
      if (typeof origFetch === "function") {
        window.fetch = function (input, init) {
          try {
            if (typeof input === "string") return origFetch(prefix(input), init);
            if (input && input.url) {
              var u = String(input.url || "");
              var pu = prefix(u);
              if (pu !== u) input = new Request(pu, input);
            }
          } catch (e) {}
          return origFetch(input, init);
        };
      }
    } catch (e) {}

    try {
      var OrigEventSource = window.EventSource;
      if (typeof OrigEventSource === "function") {
        window.EventSource = function (url, cfg) {
          return new OrigEventSource(prefix(String(url || "")), cfg);
        };
        window.EventSource.prototype = OrigEventSource.prototype;
      }
    } catch (e) {}
  })();
</script>
"""
            # Insert early so page scripts that call fetch/EventSource immediately
            # are already patched when they run (Ingress paths).
            if "</head>" in html:
                html = html.replace("</head>", shim + "\n</head>")
            else:
                html = html.replace("</body>", shim + "\n</body>")
            return html.encode("utf-8")
        except Exception:
            return body

    def _send(self, status, content_type, body: bytes):
        if isinstance(body, (bytes, bytearray)) and str(content_type).startswith("text/html"):
            body = self._inject_ingress_shim(body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        raw_path = urlparse(self.path).path

        def _split_ingress(p: str):
            try:
                p = str(p or "")
                if p.startswith("/api/hassio_ingress/"):
                    parts = p.split("/")
                    # ['', 'api', 'hassio_ingress', '<token>', ...]
                    if len(parts) >= 4 and parts[3]:
                        prefix = "/".join(parts[:4])
                        rest = "/" + "/".join(parts[4:]) if len(parts) > 4 else "/"
                        if rest == "/":
                            return prefix, "/"
                        return prefix, rest
                m = re.match(r"^(/local_[^/]+/ingress)(/.*)?$", p)
                if m:
                    prefix = m.group(1)
                    rest = m.group(2) or "/"
                    return prefix, rest
            except Exception:
                pass
            return "", p or "/"

        ingress_prefix, path = _split_ingress(raw_path)

        # On the Ingress/debug port (8080), make the root path show the launcher menu by default.
        # Do not redirect /index_debug here, otherwise the menu cannot open index_debug via Ingress.
        try:
            if int(getattr(self.server, "server_port", 0)) == 8080 and path in (
                "/",
            ):
                self.send_response(302)
                self.send_header("Location", f"{ingress_prefix}/menu" if ingress_prefix else "/menu")
                self.end_headers()
                return
        except Exception:
            pass

        # On the dedicated Security UI port, make the root path show the Security UI by default.
        try:
            if int(getattr(self.server, "server_port", 0)) == 8081 and path in (
                "/",
                "/index_debug",
                "/index_debug/",
            ):
                self.send_response(302)
                self.send_header(
                    "Location", f"{ingress_prefix}/security" if ingress_prefix else "/security"
                )
                self.end_headers()
                return
        except Exception:
            pass

        if path.startswith("/assets/"):
            name = path.split("/", 2)[2]
            name = (name or "").strip().lower()
            if name.endswith(".png"):
                name = name[:-4]
            filename = _ASSET_MAP.get(name)
            if not filename:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            file_path = os.path.join(_ASSETS_DIR, filename)
            try:
                with open(file_path, "rb") as f:
                    body = f.read()
            except Exception:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/security", "/security/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_ui(snap))
            return
        if path in ("/menu", "/menu/"):
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_menu(snap))
            return
        if path in ("/security/partitions", "/security/partitions/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_partitions(snap))
            return
        if path in ("/security/scenarios", "/security/scenarios/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_scenarios(snap))
            return
        if path in ("/security/sensors", "/security/sensors/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_sensors(snap))
            return
        if path in ("/security/reset", "/security/reset/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_reset(snap))
            return
        if path in ("/security/info", "/security/info/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_info(snap))
            return
        if path in ("/security/users", "/security/users/"):
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_users(snap))
            return
        if path in ("/security/timers", "/security/timers/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_timers(snap))
            return
        if path in ("/security/functions", "/security/functions/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_functions(snap))
            return
        if path in ("/security/functions/all", "/security/functions/all/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_functions_all(snap))
            return
        if path in ("/security/functions/outputs", "/security/functions/outputs/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_functions_outputs(snap))
            return
        if path in ("/security/favorites", "/security/favorites/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_security_favorites(snap))
            return
        if path in ("/index_debug/tag_styles", "/index_debug/tag_styles/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_index_tag_styles(snap))
            return
        if path in ("/", "/index_debug", "/index_debug/"):
            try:
                _UI_LOGGER.info("UI GET %s from %s", path, self.client_address[0])
            except Exception:
                pass
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_index(snap))
            return
        if path in ("/thermostats", "/thermostats/"):
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_thermostats(snap))
            return
        if path.startswith("/thermostats/"):
            tid = path.split("/", 2)[2] if len(path.split("/")) >= 3 else ""
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_thermostat_detail(snap, tid))
            return
        if path in ("/logs", "/logs/"):
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_logs(snap))
            return
        if path in ("/timers", "/timers/"):
            snap = self.state.snapshot()
            self._send(200, "text/html; charset=utf-8", render_timers(snap))
            return
        if path == "/api/entities":
            snap = self.state.snapshot()
            snap["ui_rev"] = UI_REV
            body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path == "/api/ui_tags":
            body = json.dumps(_load_ui_tags(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path == "/api/ui_favorites":
            try:
                with _UI_FAVORITES_LOCK:
                    favs = _load_ui_favorites()
                self._send(200, "application/json; charset=utf-8", json.dumps(favs).encode("utf-8"))
            except Exception:
                self._send(500, "text/plain; charset=utf-8", b"error")
            return
        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q = self.state.subscribe()
            try:
                snap = self.state.snapshot()
                snap["ui_rev"] = UI_REV
                self.wfile.write(
                    ("data: " + json.dumps(snap, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()

                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        # keep-alive
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(
                        ("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8")
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                pass
            finally:
                self.state.unsubscribe(q)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/ui_favorites":
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                etype = str(payload.get("type") or "").strip()
                eid = str(payload.get("id") or "").strip()
                fav = bool(payload.get("fav", False))
                if etype not in ("outputs", "scenarios", "zones", "partitions") or not eid:
                    raise ValueError("invalid payload")
                with _UI_FAVORITES_LOCK:
                    favs = _load_ui_favorites()
                    bucket = favs.get(etype, {})
                    if not isinstance(bucket, dict):
                        bucket = {}
                    if fav:
                        bucket[eid] = True
                    else:
                        bucket.pop(eid, None)
                    favs[etype] = bucket
                    _save_ui_favorites(favs)
                self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(400, "application/json; charset=utf-8", body)
            return

        if path != "/api/cmd":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        command_fn = type(self).command_fn
        if not command_fn:
            self._send(503, "text/plain; charset=utf-8", b"command handler not ready")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, "text/plain; charset=utf-8", b"invalid json")
            return
        try:
            result = command_fn(payload)
            # Back-compat: allow handlers to return bool (success/failure) or dict.
            if result is None:
                result = {"ok": True}
            elif isinstance(result, bool):
                result = {"ok": bool(result)}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            # Always return 200 to let the frontend inspect error payloads (even if ok is False).
            self._send(200, "application/json; charset=utf-8", body)
        except Exception as exc:
            body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self._send(500, "application/json; charset=utf-8", body)

    def log_message(self, format, *args):
        return


def start_debug_server(state: LaresState, host="0.0.0.0", port=8080, command_fn=None):
    _Handler.state = state
    _Handler.command_fn = command_fn
    httpd = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, name="debug_server", daemon=True)
    thread.start()
    return httpd


def set_command_handler(command_fn):
    _Handler.command_fn = command_fn


# -----------------------------------------------------------------------------
# Clean Thermostats UI (replaces older experimental versions)
# -----------------------------------------------------------------------------

def render_thermostats(snapshot):
    entities = snapshot.get("entities") or []
    therms = [e for e in entities if str(e.get("type") or "").lower() == "thermostats"]

    rows = []
    for e in therms:
        tid = e.get("id")
        name = e.get("name") or (e.get("static") or {}).get("DES") or f"Termostato {tid}"
        rows.append(
            f'<li><a href="./thermostats/{_html_escape(str(tid))}">{_html_escape(str(name))}</a> '
            f'<span class="muted">(# {_html_escape(str(tid))})</span></li>'
        )

    items = (
        "<ul>" + "".join(rows) + "</ul>"
        if rows
        else '<div class="muted">Nessun termostato trovato.</div>'
    )

    tpl = """<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - Termostati</title>
    <style>
      :root {
        --bg0: #05070b;
        --fg: #e7eaf0;
        --muted: rgba(255,255,255,0.65);
        --border: rgba(255,255,255,0.10);
        --card: rgba(0,0,0,0.28);
      }
      body {
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }
      .bg {
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }
      .topbar {
        position:fixed; top:0; left:0; right:0;
        display:flex; align-items:center; justify-content:center;
        height:72px;
        background: linear-gradient(to bottom, rgba(0,0,0,0.55), rgba(0,0,0,0));
        backdrop-filter: blur(8px);
        z-index: 2;
      }
      .back {
        position:absolute; left:12px; top:50%; transform: translateY(-50%);
        width:44px; height:44px; border-radius: 999px;
        display:flex; align-items:center; justify-content:center;
        border:1px solid rgba(255,255,255,0.10);
        background: rgba(0,0,0,0.18);
      }
      .back:hover { background: rgba(255,255,255,0.06); }
      .barRight {
        position:absolute; right:12px; top:50%; transform: translateY(-50%);
      }
      .barTitle { font-size: 18px; letter-spacing: 0.5px; color: rgba(255,255,255,0.88); }
      a { color: var(--fg); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .wrap { max-width: 980px; margin: 0 auto; padding: 92px 16px 48px; }
      .badge { display:inline-block; padding:2px 10px; border:1px solid var(--border); border-radius: 999px; color: var(--muted); }
      .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; margin-top: 12px; }
      .muted { color: var(--muted); }
      ul { margin: 8px 0 0; padding-left: 18px; }
      li { margin: 6px 0; }
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="/index_debug" title="index_debug" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <div class="barTitle">Termostati</div>
      <div class="barRight">
        <span class="badge">v __ADDON_VERSION__</span>
        <span class="badge">UI __UI_REV__</span>
      </div>
    </div>
    <div class="wrap">
      <div class="card">
        __ITEMS__
      </div>
    </div>
  </body>
</html>"""

    html = (
        tpl.replace("__ADDON_VERSION__", _html_escape(ADDON_VERSION))
        .replace("__UI_REV__", _html_escape(UI_REV))
        .replace("__ITEMS__", items)
    )
    return html.encode("utf-8")


def render_thermostat_detail(snapshot, thermostat_id: str):
    # Clean, stable base page (we'll iterate design step-by-step).
    title = f"Termostato {thermostat_id}"
    for e in (snapshot.get("entities") or []):
        if str(e.get("type") or "").lower() == "thermostats" and str(e.get("id")) == str(thermostat_id):
            title = e.get("name") or title
            break

    init = json.dumps(snapshot, ensure_ascii=False)
    tid_esc = _html_escape(str(thermostat_id))

    tpl = """<!doctype html>
<html lang="it">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="Cache-Control" content="no-store, max-age=0"/>
    <meta http-equiv="Pragma" content="no-cache"/>
    <meta http-equiv="Expires" content="0"/>
    <title>Ksenia Lares - __TITLE__</title>
    <style>
       :root {
         --bg0: #05070b;
         --fg: #e7eaf0;
         --muted: rgba(255,255,255,0.65);
         --ring-w: 14px;
         --ring-track: rgba(255,255,255,0.14);
         --ring-off: rgba(255,255,255,0.22);
         --ring-heat: #ff9800;
         --ring-cool: #2a7fff;
         --bd: rgba(255,255,255,0.10);
         --accent: var(--ring-off);
         --pin-fg: rgba(255,255,255,0.92);
      }
      html, body { height:100%; }
      body {
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
        color: var(--fg);
        background: radial-gradient(1200px 800px at 50% 50%, #1a2230 0%, var(--bg0) 60%, #000 100%);
      }
      .bg {
        position:fixed; inset:0;
        background: radial-gradient(900px 600px at 50% 50%, rgba(255,255,255,0.08), rgba(0,0,0,0.55));
        filter: blur(28px);
        opacity: 0.85;
        pointer-events:none;
      }
      .topbar {
        position:fixed; top:0; left:0; right:0;
        display:flex; gap:18px; justify-content:center; align-items:center;
        height:72px;
        background: linear-gradient(to bottom, rgba(0,0,0,0.55), rgba(0,0,0,0));
        backdrop-filter: blur(8px);
        z-index: 2;
      }
      .back {
        position:absolute; left:12px; top:50%; transform: translateY(-50%);
        width:44px; height:44px; border-radius: 999px;
        display:flex; align-items:center; justify-content:center;
        border:1px solid var(--bd);
        background: rgba(0,0,0,0.18);
        color: rgba(255,255,255,0.88);
        text-decoration:none;
      }
      .back:hover { background: rgba(255,255,255,0.06); }
      .barTitle { font-size: 18px; letter-spacing: 0.5px; color: rgba(255,255,255,0.88); }
      .barRight {
        position:absolute; right:12px; top:50%; transform: translateY(-50%);
        display:flex; gap:10px; align-items:center;
      }
      .wrap { max-width: 1220px; margin: 0 auto; padding: 92px 16px 48px; }
      .meta { display:none; }
      .badge { display:inline-block; padding:2px 10px; border:1px solid var(--bd); border-radius: 999px; color: var(--muted); background: rgba(0,0,0,0.14); font-size:12px; }
      .muted { color: var(--muted); }
      .top, .chips, .grid { display:none; }

      .layout { display:flex; align-items:center; justify-content:center; gap: min(9vw, 120px); margin-top: 0; min-height: calc(100vh - 160px); }
      @media (max-width: 980px) { .layout { flex-direction:column; gap: 18px; min-height: auto; padding-top: 8px; } }
      .ringWrap { position: relative; width: min(70vw, 560px); height: min(70vw, 560px); margin: 0 auto; touch-action: none; }
      .ringSvg { position:absolute; inset:0; transform: rotate(-90deg); }
      .ringTick { position:absolute; left:50%; top: 10px; width: 4px; height: calc(var(--ring-w) * 0.9); border-radius: 999px; background: rgba(255,255,255,0.86); transform: translate(-50%,-50%); box-shadow: 0 10px 26px rgba(0,0,0,0.55); pointer-events:none; }
      .ringCenter { position:absolute; inset: 15%; border-radius: 999px; background: radial-gradient(220px 220px at 40% 35%, rgba(255,255,255,0.14), rgba(0,0,0,0.42)); border: 1px solid var(--bd); box-shadow: 0 18px 60px rgba(0,0,0,0.65); display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; gap: 10px; user-select:none; }
      .big { font-size: clamp(64px, 10vw, 112px); font-weight: 300; letter-spacing: 0.5px; line-height: 1; }
      .sub { font-size: 14px; color: rgba(255,255,255,0.72); }
      .sub2 { font-size: 12px; color: rgba(255,255,255,0.62); }
      .btnRow, .roundBtn, .side, .sideCard, .sideHead, .sideBody, .actionBtn, .aLeft, .aTxt, .aName, .aVal { display:none; }
      .ico { width: 56px; height: 56px; border-radius: 999px; display:flex; align-items:center; justify-content:center; border: 1px solid rgba(255,255,255,0.12); background: rgba(0,0,0,0.18); color: rgba(255,255,255,0.86); }

      .knob { position:absolute; width: var(--ring-w); height: var(--ring-w); border-radius: 999px; border: 2px solid rgba(255,255,255,0.88); background: rgba(0,0,0,0.55); box-shadow: 0 10px 30px rgba(0,0,0,0.55); transform: translate(-50%, -50%); cursor: pointer; }
      .knobPin {
         position:absolute;
         left:50%;
         top:0;
         transform: translate(-50%, -110%);
         width: 74px;
         height: 54px;
         border-radius: 999px;
        background: linear-gradient(to bottom, rgba(255,255,255,0.18), rgba(0,0,0,0.08)), var(--accent);
        color: var(--pin-fg);
         display:flex;
         align-items:center;
         justify-content:center;
         font-size: 22px;
         letter-spacing: 0.2px;
         box-shadow: 0 14px 40px rgba(0,0,0,0.55);
       }
      .knobPin:after {
        content:'';
        position:absolute;
        left:50%;
        bottom:-10px;
        transform: translateX(-50%) rotate(45deg);
        width: 18px;
        height: 18px;
        background: var(--accent);
        border-radius: 4px;
      }
      .knob.dragging { filter: drop-shadow(0 0 12px rgba(255,255,255,0.12)); }

      .sideCol { display:flex; flex-direction:column; align-items:center; justify-content:center; gap: 34px; min-width: 150px; }
      @media (max-width: 980px) { .sideCol { flex-direction:row; min-width: auto; gap: 20px; } }
      .sideAction { width: 120px; display:flex; flex-direction:column; align-items:center; gap: 10px; cursor: pointer; user-select:none; }
      .sideAction:hover .ico { background: rgba(255,255,255,0.06); }
      .sideAction .lab { font-size: 13px; color: rgba(255,255,255,0.72); text-align:center; }
      .sideAction .val { font-size: 12px; color: rgba(255,255,255,0.55); text-align:center; margin-top: -6px; }

      .panel { margin-top: 16px; border: 1px solid var(--bd); border-radius: 18px; background: rgba(0,0,0,0.14); overflow:hidden; display:none; }
      .panel.show { display:block; }
      .panelHead { padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,0.08); display:flex; align-items:center; justify-content:space-between; gap: 10px; }
      .panelBody { padding: 12px 14px; }
      table { border-collapse: collapse; width: 100%; font-size: 12px; }
      th, td { border-bottom: 1px solid rgba(255,255,255,0.10); padding: 6px 6px; }
      th { text-align: left; position: sticky; top: 0; background: rgba(0,0,0,0.35); }
      td.h { width: 42px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
      select, input { background: rgba(0,0,0,0.25); color: var(--fg); border:1px solid rgba(255,255,255,0.12); border-radius: 10px; padding: 7px 10px; }
      .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
      .btn { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); color: var(--fg); border-radius: 10px; padding: 7px 10px; cursor:pointer; }
      .btn:hover { background: rgba(255,255,255,0.10); }

      pre { margin:0; white-space: pre; max-height: 360px; overflow:auto; }
      .toast { position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); background: rgba(0,0,0,0.65); border: 1px solid rgba(255,255,255,0.10); color: rgba(255,255,255,0.92); padding: 10px 14px; border-radius: 12px; display:none; z-index: 30; min-width: 180px; text-align: center; }
      dialog { border: 1px solid rgba(255,255,255,0.12); border-radius: 16px; background: rgba(15,18,26,0.96); color: rgba(255,255,255,0.92); padding: 0; }
      dialog::backdrop { background: rgba(0,0,0,0.55); }
      .dlgHead { padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,0.08); display:flex; align-items:center; justify-content:space-between; gap: 10px; }
      .dlgTitle { font-size: 15px; }
      .dlgList { padding: 8px; }
      .dlgItem { display:flex; align-items:center; justify-content:space-between; gap: 10px; padding: 12px 12px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.03); cursor: pointer; margin: 8px; }
      .dlgItem:hover { background: rgba(255,255,255,0.06); }
    </style>
  </head>
  <body>
    <div class="bg"></div>
    <button class="refreshBtn" id="refreshBtn" type="button" title="Aggiorna stato" aria-label="Aggiorna"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 1 1-9.9-1H5.02a7 7 0 1 0 12.63-4.65z"/></svg></button>
    <div class="topbar">
      <a class="back" href="../thermostats" title="Termostati" aria-label="Indietro">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </a>
      <div class="barTitle">__TITLE__</div>
      <div class="barRight">
        <span class="badge">ID __TID__</span>
        <span class="badge">v __ADDON_VERSION__</span>
      </div>
    </div>

    <div class="wrap">
      <div class="layout">
        <div class="sideCol">
          <div class="sideAction" id="btnSeason" role="button" tabindex="0">
            <div class="ico" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="7.5" stroke="currentColor" stroke-width="2"></circle>
              </svg>
            </div>
            <div class="lab" id="labSeason">Modalità</div>
            <div class="val" id="valSeason"></div>
          </div>

          <div class="sideAction" id="btnExtra" role="button" tabindex="0">
            <div class="ico" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M4 7h10M4 12h16M4 17h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"></path>
                <circle cx="16" cy="7" r="2" stroke="currentColor" stroke-width="2"></circle>
                <circle cx="8" cy="17" r="2" stroke="currentColor" stroke-width="2"></circle>
              </svg>
            </div>
            <div class="lab">Extra</div>
            <div class="val" id="valExtra">-</div>
          </div>
        </div>

        <div>
           <div class="ringWrap" id="ringWrap">
             <svg class="ringSvg" viewBox="0 0 200 200" aria-hidden="true">
               <circle id="ringTrack" cx="100" cy="100" r="84" fill="none" stroke="var(--ring-track)" stroke-width="var(--ring-w)" stroke-linecap="round"></circle>
               <circle id="ringFg" cx="100" cy="100" r="84" fill="none" stroke="var(--ring-off)" stroke-width="var(--ring-w)" stroke-linecap="round" stroke-dasharray="1 999"></circle>
             </svg>
             <div class="ringTick" id="ringTick" aria-hidden="true"></div>
             <div class="knob" id="knob" role="slider" tabindex="0" aria-label="Set temperatura">
               <div class="knobPin"><span id="knobVal">--,-</span></div>
             </div>
            <div class="ringCenter">
              <div class="sub" id="centerSub">-</div>
              <div class="big" id="centerTemp">--,-</div>
              <div class="sub" id="centerSet">Set --,-&deg;C</div>
              <div class="sub2" id="centerRh">RH --%</div>
            </div>
          </div>
        </div>

        <div class="sideCol">
          <div class="sideAction" id="btnMode" role="button" tabindex="0">
            <div class="ico" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"></path>
              </svg>
            </div>
            <div class="lab">Preset</div>
            <div class="val" id="valMode"></div>
          </div>

          <div class="sideAction" id="btnSchedule" role="button" tabindex="0">
            <div class="ico" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                <rect x="3" y="4" width="18" height="18" rx="3" stroke="currentColor" stroke-width="2"></rect>
                <path d="M16 2v4M8 2v4M3 10h18" stroke="currentColor" stroke-width="2" stroke-linecap="round"></path>
              </svg>
            </div>
            <div class="lab">Scheduler</div>
            <div class="val"></div>
          </div>
        </div>
      </div>
      <div class="top">
        <div>
          <h2 style="margin:0">__TITLE__ <span class="badge">ID __TID__</span> <span class="badge">v__ADDON_VERSION__</span></h2>
          <div class="muted" style="margin-top:6px">
            <a href="../thermostats">&larr; Termostati</a> &bull; <a href="../index_debug">index_debug</a>
            &bull; Aggiornato: <span id="lastUpdateOld">-</span>
          </div>
        </div>
        <div class="chips">
          <div class="chip"><span class="k">Temp</span> <span id="chipTemp">-</span></div>
          <div class="chip"><span class="k">RH</span> <span id="chipRh">-</span></div>
          <div class="chip"><span class="k">Uscita</span> <span id="chipOut">-</span></div>
          <div class="chip"><span class="k">Stagione</span> <span id="chipSeason">-</span></div>
          <div class="chip"><span class="k">Modo</span> <span id="chipMode">-</span></div>
        </div>
      </div>

      <div class="panel" id="panelSchedule">
        <div class="panelHead">
          <div class="muted">Schedulazione</div>
          <button class="btn" id="btnScheduleClose" type="button">Chiudi</button>
        </div>
        <div class="panelBody">
        <div class="card">
          <div class="row">
            <label class="muted">Stagione</label>
            <select id="schedSeason">
              <option value="WIN">Inverno</option>
              <option value="SUM">Estate</option>
            </select>
          </div>

          <div style="margin-top:12px" class="muted">Programmazione oraria</div>
          <div class="row" style="margin-top:8px">
            <label class="muted">Tabella</label>
            <select id="schedTable">
              <option value="MON">MON</option>
              <option value="TUE">TUE</option>
              <option value="WED">WED</option>
              <option value="THU">THU</option>
              <option value="FRI">FRI</option>
              <option value="SAT">SAT</option>
              <option value="SUN">SUN</option>
              <option value="SD1">SD1</option>
              <option value="SD2">SD2</option>
            </select>
            <button class="btn" id="reloadBtn" type="button">Ricarica</button>
          </div>
          <div style="margin-top:10px; overflow:auto; max-height: 520px">
            <table id="schedTbl">
              <thead><tr><th>h</th><th>Profilo</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        </div>
      </div>
    </div>

    <dialog id="picker">
      <div class="dlgList" id="pickerList"></div>
    </dialog>

    <dialog id="extraDlg">
      <div class="dlgList" style="padding: 14px 14px 10px;">
        <div class="muted" style="margin: 0 6px 10px;">Extra</div>
        <div class="row" style="gap:10px">
          <input id="extraNameInp" placeholder="Nome termostato..." style="flex:1; min-width: 220px"/>
          <button class="btn" id="extraNameSave" type="button">Salva</button>
        </div>
        <div class="muted" style="margin: 14px 6px 8px;">Profili</div>
        <div class="row" style="gap:12px; align-items:center; margin: 0 6px 10px;">
          <span class="badge" id="extraSeasonBadge">-</span>
        </div>
        <div class="row" style="gap:12px; align-items:center; margin: 0 6px 10px;">
          <span class="badge" style="min-width:42px; text-align:center;">T1</span>
          <input id="extraT1" type="range" min="5" max="35" step="0.5" value="20" style="flex:1"/>
          <span class="badge" id="extraT1Val" style="min-width:62px; text-align:center;">--</span>
        </div>
        <div class="row" style="gap:12px; align-items:center; margin: 0 6px 10px;">
          <span class="badge" style="min-width:42px; text-align:center;">T2</span>
          <input id="extraT2" type="range" min="5" max="35" step="0.5" value="20" style="flex:1"/>
          <span class="badge" id="extraT2Val" style="min-width:62px; text-align:center;">--</span>
        </div>
        <div class="row" style="gap:12px; align-items:center; margin: 0 6px 10px;">
          <span class="badge" style="min-width:42px; text-align:center;">T3</span>
          <input id="extraT3" type="range" min="5" max="35" step="0.5" value="20" style="flex:1"/>
          <span class="badge" id="extraT3Val" style="min-width:62px; text-align:center;">--</span>
        </div>
        <div class="row" style="gap:12px; align-items:center; margin: 0 6px 0;">
          <span class="badge" style="min-width:42px; text-align:center;">TM</span>
          <input id="extraTM" type="range" min="5" max="35" step="0.5" value="20" style="flex:1"/>
          <span class="badge" id="extraTMVal" style="min-width:62px; text-align:center;">--</span>
        </div>
      </div>
    </dialog>

    <div class="toast" id="toast"></div>
    <script>
      const TH_ID = String("__TID__");
      let snap = __INIT__;
      let sse = null;

      function apiRoot() {
        const p = String(window.location && window.location.pathname ? window.location.pathname : "");
        if (p.startsWith("/api/hassio_ingress/")) {
          const parts = p.split("/").filter(Boolean);
          if (parts.length >= 3) return "/" + parts.slice(0, 3).join("/");
        }
        return "";
      }
      function apiUrl(path) {
        const root = apiRoot();
        const p = String(path || "");
        if (p.startsWith("/")) return root + p;
        return root + "/" + p;
      }

      function toast(msg) {
        const el = document.getElementById("toast");
        if (!el) return;
        el.textContent = String(msg || "");
        el.style.display = "block";
        clearTimeout(toast._t);
        toast._t = setTimeout(() => { el.style.display = "none"; }, 1400);
      }

      function getTherm() {
        const ents = (snap && snap.entities) ? snap.entities : [];
        for (const e of ents) {
          if (String(e.type || "").toLowerCase() === "thermostats" && String(e.id) === TH_ID) return e;
        }
        return null;
      }

      function fmtDec(s) {
        const n = Number(String(s || "").replace(",", "."));
        if (!Number.isFinite(n)) return "";
        return n.toFixed(1);
      }

      function ringSetColor(outOn, season) {
        const fg = document.getElementById("ringFg");
        if (!fg) return;
        const root = document.documentElement;
        let accent = "var(--ring-off)";
        if (outOn) accent = (season === "SUM") ? "var(--ring-cool)" : "var(--ring-heat)";
        fg.style.stroke = accent;
        if (root) {
          root.style.setProperty("--accent", accent);
          root.style.setProperty("--pin-fg", outOn ? "rgba(0,0,0,0.86)" : "rgba(255,255,255,0.92)");
        }
      }
      function ringSetValue(val) {
        const fg = document.getElementById("ringFg");
        if (!fg) return;
        const r = 84;
        const C = 2 * Math.PI * r;
        let n = Number(String(val || "").replace(",", "."));
        if (!Number.isFinite(n)) n = 20;
        n = Math.max(5, Math.min(35, n));
        const pct = (n - 5) / 30;
        const dash = Math.max(0.01, Math.min(0.999, pct)) * C;
        fg.setAttribute("stroke-dasharray", String(dash.toFixed(2)) + " " + String((C - dash).toFixed(2)));
      }

      function openPicker(items, activeValue, onPick) {
        const dlg = document.getElementById("picker");
        const list = document.getElementById("pickerList");
        if (!dlg || !list) return;
        list.innerHTML = (items || []).map((it) => {
          const active = (activeValue !== null && activeValue !== undefined && String(it.value) === String(activeValue));
          return '<div class="dlgItem" data-v="' + String(it.value) + '">' +
            '<div>' + String(it.label) + '</div>' +
            (active ? '<div class="badge">attivo</div>' : (it.hint ? '<div class="badge">' + String(it.hint) + '</div>' : '<div></div>')) +
          '</div>';
        }).join("");
        list.querySelectorAll(".dlgItem[data-v]").forEach((el) => {
          el.onclick = () => {
            const v = el.getAttribute("data-v");
            try { onPick(v); } catch (_e) {}
            dlg.close();
          };
        });
        dlg.showModal();
      }

      function wireBtn(id, fn) {
        const el = document.getElementById(id);
        if (!el) return;
        el.onclick = fn;
        el.onkeydown = (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fn(); } };
      }

      // Close dialogs when clicking outside
      try {
        const dlg = document.getElementById("picker");
        if (dlg) dlg.addEventListener("click", (ev) => { if (ev.target === dlg) dlg.close(); });
      } catch (_e) {}
      try {
        const dlg2 = document.getElementById("extraDlg");
        if (dlg2) dlg2.addEventListener("click", (ev) => { if (ev.target === dlg2) dlg2.close(); });
      } catch (_e) {}

      async function fetchSnap() {
        try {
          const res = await fetch(apiUrl("/api/entities"), { cache: "no-store" });
          if (!res.ok) return;
          snap = await res.json();
          render();
        } catch (_e) {}
      }

      async function sendCmd(action, value=null) {
        const payload = { type: "thermostats", id: Number(TH_ID), action: String(action) };
        if (value !== null && value !== undefined) payload.value = value;
        const res = await fetch(apiUrl("/api/cmd"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        let data = null;
        try { data = await res.json(); } catch (_e) { data = null; }
        if (!res.ok || !data || data.ok !== true) {
          const err = (data && data.error) ? data.error : String(res.status);
          throw new Error(err);
        }
        toast("OK");
        const delay = (String(action) === "set_target" || String(action) === "set_profile" || String(action) === "write_patch") ? 1500 : 350;
        setTimeout(fetchSnap, delay);
      }

      function renderSchedule(ent) {
        const tbody = document.querySelector("#schedTbl tbody");
        if (!tbody) return;
        tbody.innerHTML = "";
        if (!ent) return;
        const st = ent.static || {};
        const season = String((document.getElementById("schedSeason") || {}).value || "WIN").toUpperCase();
        const dayKey = String((document.getElementById("schedTable") || {}).value || "MON").toUpperCase();
        const sea = (season === "SUM" || season === "WIN") ? st[season] : null;
        const arr = sea && Array.isArray(sea[dayKey]) ? sea[dayKey] : null;
        for (let h = 0; h < 24; h++) {
          const cur = (arr && arr[h] && typeof arr[h] === "object") ? arr[h] : null;
          const t = cur ? String(cur.T || "") : "";
          const tr = document.createElement("tr");
          tr.innerHTML =
            '<td class="h">' + String(h) + '</td>' +
            '<td><select data-h="' + String(h) + '">' +
              '<option value="1"' + (t === "1" ? " selected" : "") + '>T1</option>' +
              '<option value="2"' + (t === "2" ? " selected" : "") + '>T2</option>' +
              '<option value="3"' + (t === "3" ? " selected" : "") + '>T3</option>' +
            '</select></td>';
          tbody.appendChild(tr);
        }
        tbody.querySelectorAll("select[data-h]").forEach((sel) => {
          sel.addEventListener("change", async () => {
            const hour = Number(sel.dataset.h);
            const val = String(sel.value || "");
            try {
              await sendCmd("set_schedule", { season: season, day: dayKey, hour: hour, t: val });
            } catch (e) {
              toast("Errore: " + String(e && e.message ? e.message : e));
            }
          });
        });
      }

      function render() {
        const ent = getTherm();
        const meta = (snap && snap.meta && typeof snap.meta === "object") ? snap.meta : null;
        const last = meta && meta.last_update ? Number(meta.last_update) : 0;
        const lastStr = last ? new Date(last * 1000).toISOString().replace("T", " ").slice(0, 19) : "-";
        const lastEl = document.getElementById("lastUpdate");
        if (lastEl) lastEl.textContent = lastStr;

        const rtPre = document.getElementById("rt");
        const stPre = document.getElementById("st");
        if (rtPre) rtPre.textContent = ent && ent.realtime ? JSON.stringify(ent.realtime, null, 2) : "-";
        if (stPre) stPre.textContent = ent && ent.static ? JSON.stringify(ent.static, null, 2) : "-";

        if (!ent) return;

        const rt = ent.realtime || {};
        const therm = (rt.THERM && typeof rt.THERM === "object") ? rt.THERM : null;
        const mode = therm ? String(therm.ACT_MODEL || therm.ACT_MODE || "") : "";
        const season = therm ? String(therm.ACT_SEA || "") : "";
        const out = therm ? String(therm.OUT_STATUS || "") : "";
        const temp = (rt.TEMP !== undefined && rt.TEMP !== null) ? String(rt.TEMP) : "";
        const rh = (rt.RH !== undefined && rt.RH !== null) ? String(rt.RH) : "";
        const thr = therm && therm.TEMP_THR && typeof therm.TEMP_THR === "object" ? therm.TEMP_THR : null;
        const target = (thr && thr.VAL !== undefined && thr.VAL !== null) ? String(thr.VAL) : "";

        // New ring UI (same visual style as Security UI)
        const seaKey = (String(season || "WIN").toUpperCase() === "SUM") ? "SUM" : "WIN";
        const outOn = String(out || "").toUpperCase() === "ON";
        const tempDisp = temp ? fmtDec(temp).replace(".", ",") : "--,-";
        const rhDisp = rh ? (String(rh) + "%") : "--%";
        const targetNum = target ? Number(fmtDec(target)) : NaN;
        const now = Date.now();
        if (pendingTarget && (now - pendingTarget.ts) > 30000) pendingTarget = null;
        if (pendingTarget && Number.isFinite(targetNum) && Math.abs(targetNum - pendingTarget.val) < 0.05) pendingTarget = null;
        const effTarget = pendingTarget ? pendingTarget.val : (Number.isFinite(targetNum) ? targetNum : NaN);
        const setDisp = Number.isFinite(effTarget) ? String(effTarget.toFixed(1)).replace(".", ",") : "--,-";
        const modeDisp = mode || "-";
        const seaLabel = (seaKey === "SUM") ? "Freddo" : "Caldo";
        const titleLine = ((seaKey === "SUM") ? "Estate" : "Inverno") + " | " + modeDisp;

        const elSub = document.getElementById("centerSub");
        const elTemp = document.getElementById("centerTemp");
        const elSet = document.getElementById("centerSet");
        const elRh = document.getElementById("centerRh");
        const elOut = document.getElementById("badgeOut");
        const elSea = document.getElementById("valSeason");
        const elMode = document.getElementById("valMode");
        if (elSub) elSub.textContent = titleLine;
        if (elTemp) elTemp.textContent = tempDisp;
        if (elSet) elSet.innerHTML = "Set " + setDisp + "&deg;C";
        if (elRh) elRh.textContent = "RH " + rhDisp;
        if (elOut) elOut.textContent = "OUT: " + (out || "-");
        if (elSea) elSea.textContent = "";
        if (elMode) elMode.textContent = "";
        const elKnobVal = document.getElementById("knobVal");
        if (elKnobVal && !dialDragging) elKnobVal.textContent = setDisp;

        const extraNameInp = document.getElementById("extraNameInp");
        if (extraNameInp && !extraNameInp._dirty) extraNameInp.value = String(ent.name || "");
        const extraSeasonBadge = document.getElementById("extraSeasonBadge");
        if (extraSeasonBadge) extraSeasonBadge.textContent = (seaKey === "SUM") ? "Estate" : "Inverno";
        const stcfg = ent.static || {};
        const prof = (stcfg && stcfg[seaKey] && typeof stcfg[seaKey] === "object") ? stcfg[seaKey] : null;
        const getPendingProfile = (key) => {
          const k = String(seaKey) + ":" + String(key);
          const p = pendingProfiles[k];
          if (!p) return null;
          if ((Date.now() - Number(p.ts || 0)) > 30000) { delete pendingProfiles[k]; return null; }
          return p;
        };
        const maybeClearPending = (key, liveVal) => {
          const k = String(seaKey) + ":" + String(key);
          const p = pendingProfiles[k];
          if (!p) return;
          const n = Number(String(liveVal || "").replace(",", "."));
          if (Number.isFinite(n) && Math.abs(n - Number(p.val)) < 0.05) delete pendingProfiles[k];
        };
        const setRangeIfClean = (rngId, valId, v, key) => {
          const rng = document.getElementById(rngId);
          const out = document.getElementById(valId);
          const pend = key ? getPendingProfile(key) : null;
          const src = pend ? pend.val : v;
          const n = Number(String(src || "").replace(",", "."));
          if (out) out.textContent = Number.isFinite(n) ? (n.toFixed(1).replace(".", ",") + "\u00B0") : "--";
          if (rng && !rng._dirty && Number.isFinite(n)) rng.value = String(n);
        };
        maybeClearPending("T1", prof ? prof.T1 : null);
        maybeClearPending("T2", prof ? prof.T2 : null);
        maybeClearPending("T3", prof ? prof.T3 : null);
        maybeClearPending("TM", prof ? prof.TM : null);
        setRangeIfClean("extraT1", "extraT1Val", prof ? prof.T1 : null, "T1");
        setRangeIfClean("extraT2", "extraT2Val", prof ? prof.T2 : null, "T2");
        setRangeIfClean("extraT3", "extraT3Val", prof ? prof.T3 : null, "T3");
        setRangeIfClean("extraTM", "extraTMVal", prof ? prof.TM : null, "TM");
        const elExtra = document.getElementById("valExtra");
        if (elExtra) elExtra.textContent = "";

        ringSetColor(outOn, seaKey);
        ringSetValue(Number.isFinite(effTarget) ? String(effTarget.toFixed(1)) : (target ? fmtDec(target) : "20"));
        dialSetKnob(Number.isFinite(effTarget) ? effTarget : (target ? Number(fmtDec(target)) : 20));
        if (temp) tickSet(Number(fmtDec(temp)));

        const chipTemp = document.getElementById("chipTemp");
        const chipRh = document.getElementById("chipRh");
        const chipOut = document.getElementById("chipOut");
        const chipSeason = document.getElementById("chipSeason");
        const chipMode = document.getElementById("chipMode");
        if (chipTemp) chipTemp.textContent = temp ? (fmtDec(temp).replace(".", ",") + "\u00B0C") : "-";
        if (chipRh) chipRh.textContent = rh ? (String(rh) + "%") : "-";
        if (chipOut) chipOut.textContent = out || "-";
        if (chipSeason) chipSeason.textContent = season || "-";
        if (chipMode) chipMode.textContent = mode || "-";

        const schedSeason = document.getElementById("schedSeason");
        if (schedSeason && season) schedSeason.value = String(season).toUpperCase();

        // (legacy debug/profile controls removed)

        renderSchedule(ent);
      }

      function startSSE() {
        try {
          if (typeof EventSource === "undefined") return false;
          const es = new EventSource(apiUrl("/api/stream"));
          sse = es;
          es.onmessage = (ev) => {
            try {
              const msg = JSON.parse(ev.data);
              if (msg && msg.entities) snap = msg;
              if (msg && msg.type === "update") {
                if (msg.meta && snap.meta) Object.assign(snap.meta, msg.meta);
                if (Array.isArray(msg.entities)) {
                  const map = new Map();
                  for (const e of (snap.entities || [])) map.set(String(e.type) + ":" + String(e.id), e);
                  for (const e of msg.entities) map.set(String(e.type) + ":" + String(e.id), e);
                  snap.entities = Array.from(map.values());
                }
              }
              render();
            } catch (_e) {}
          };
          es.onerror = () => {
            try { es.close(); } catch (_e) {}
            sse = null;
          };
          return true;
        } catch (_e) {
          return false;
        }
      }

      // Wiring

      function toggleSchedule(show) {
        const p = document.getElementById("panelSchedule");
        if (!p) return;
        if (show) p.classList.add("show");
        else p.classList.remove("show");
      }
      toggleSchedule(false);

      // Circular slider (drag on ring)
      const ringWrap = document.getElementById("ringWrap");
      const knob = document.getElementById("knob");
      const knobVal = document.getElementById("knobVal");
      const ringTick = document.getElementById("ringTick");
      let dialDragging = false;
      let dialValue = null;
      let pendingTarget = null; // { val: number, ts: ms }
      const pendingProfiles = {}; // key: "WIN:T1" -> {val:number, ts:number}

      function clamp01(x) { return Math.max(0, Math.min(1, x)); }
      function round05(x) { return Math.round(x * 2) / 2; }

      function dialValueFromEvent(ev) {
        if (!ringWrap) return 20;
        const r = ringWrap.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const x = (ev.clientX !== undefined) ? ev.clientX : (ev.touches && ev.touches[0] ? ev.touches[0].clientX : cx);
        const y = (ev.clientY !== undefined) ? ev.clientY : (ev.touches && ev.touches[0] ? ev.touches[0].clientY : cy);
        const dx = x - cx;
        const dy = y - cy;
        const ang = Math.atan2(dy, dx); // -PI..PI, 0 on +x
        // Convert to 0..360 where 0 is top, clockwise
        const degFromTop = (ang * 180 / Math.PI + 90 + 360) % 360;
        const pct = clamp01(degFromTop / 360);
        const val = 5 + pct * 30;
        return round05(val);
      }

      function dialSetKnob(val) {
        if (!ringWrap || !knob) return;
        const rect = ringWrap.getBoundingClientRect();
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        const pct = clamp01((val - 5) / 30);
        const deg = pct * 360 - 90; // align with rotated ring
        const rad = deg * Math.PI / 180;
        let ringW = 14;
        try {
          ringW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ring-w")) || ringW;
        } catch (_e) {}
        const radius = Math.max(10, rect.width / 2 - (ringW / 2));
        const x = cx + radius * Math.cos(rad);
        const y = cy + radius * Math.sin(rad);
        knob.style.left = String(x) + "px";
        knob.style.top = String(y) + "px";
      }

      function tickSet(val) {
        if (!ringWrap || !ringTick) return;
        const rect = ringWrap.getBoundingClientRect();
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        let ringW = 14;
        try {
          ringW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ring-w")) || ringW;
        } catch (_e) {}
        let v = Number(val);
        if (!Number.isFinite(v)) return;
        v = Math.max(5, Math.min(35, v));
        const pct = clamp01((v - 5) / 30);
        const deg = pct * 360 - 90;
        const rad = deg * Math.PI / 180;
        const radius = Math.max(10, rect.width / 2 - ringW - 14);
        const x = cx + radius * Math.cos(rad);
        const y = cy + radius * Math.sin(rad);
        ringTick.style.left = String(x) + "px";
        ringTick.style.top = String(y) + "px";
        // Rotate so the tick is radial (perpendicular to the circle).
        ringTick.style.transform = "translate(-50%,-50%) rotate(" + String((deg + 90).toFixed(1)) + "deg)";
      }

      function dialPreview(val) {
        const v = String(val.toFixed(1)).replace(".", ",");
        if (knobVal) knobVal.textContent = v;
        const elSet = document.getElementById("centerSet");
        if (elSet) elSet.innerHTML = "Set " + v + "&deg;C";
        ringSetValue(String(val));
        dialSetKnob(val);
      }

      function dialCommit(val) {
        pendingTarget = { val: Number(val), ts: Date.now() };
        sendCmd("set_target", String(val.toFixed(1))).catch(e => toast("Errore: " + (e && e.message ? e.message : e)));
      }

      function dialPointerDown(ev) {
        if (!ringWrap || !knob) return;
        dialDragging = true;
        knob.classList.add("dragging");
        dialValue = dialValueFromEvent(ev);
        dialPreview(dialValue);
        try { ringWrap.setPointerCapture(ev.pointerId); } catch (_e) {}
      }
      function dialPointerMove(ev) {
        if (!dialDragging) return;
        dialValue = dialValueFromEvent(ev);
        dialPreview(dialValue);
      }
      function dialPointerUp(_ev) {
        if (!dialDragging) return;
        dialDragging = false;
        if (knob) knob.classList.remove("dragging");
        if (dialValue !== null) dialCommit(dialValue);
      }

      if (ringWrap) {
        ringWrap.addEventListener("pointerdown", dialPointerDown);
        ringWrap.addEventListener("pointermove", dialPointerMove);
        ringWrap.addEventListener("pointerup", dialPointerUp);
        ringWrap.addEventListener("pointercancel", dialPointerUp);
      }
      if (knob) {
        knob.addEventListener("pointerdown", dialPointerDown);
      }

      wireBtn("btnSchedule", () => {
        const p = document.getElementById("panelSchedule");
        const show = !(p && p.classList.contains("show"));
        toggleSchedule(show);
        if (show) {
          renderSchedule(getTherm());
          try { p.scrollIntoView({behavior:"smooth", block:"start"}); } catch (_e) {}
        }
      });
      wireBtn("btnSeason", () => {
        const ent = getTherm();
        const rt = ent ? (ent.realtime || {}) : {};
        const therm = (rt.THERM && typeof rt.THERM === "object") ? rt.THERM : null;
        const cur = therm ? String(therm.ACT_SEA || "WIN").toUpperCase() : "WIN";
        openPicker([
          { value: "WIN", label: "Inverno", hint: "Caldo" },
          { value: "SUM", label: "Estate", hint: "Freddo" },
        ], cur, (v) => sendCmd("set_season", v).catch(e => toast("Errore: " + (e && e.message ? e.message : e))));
      });
      wireBtn("btnMode", () => {
        const ent = getTherm();
        const rt = ent ? (ent.realtime || {}) : {};
        const therm = (rt.THERM && typeof rt.THERM === "object") ? rt.THERM : null;
        const cur = therm ? String(therm.ACT_MODEL || therm.ACT_MODE || "OFF").toUpperCase() : "OFF";
        openPicker([
          { value: "OFF", label: "Off" },
          { value: "MAN", label: "Manuale" },
          { value: "MAN_TMR", label: "Manuale (timer)" },
          { value: "WEEKLY", label: "Auto (settimanale)" },
          { value: "SD1", label: "SD1" },
          { value: "SD2", label: "SD2" },
        ], cur, (v) => sendCmd("set_mode", v).catch(e => toast("Errore: " + (e && e.message ? e.message : e))));
      });

      wireBtn("btnExtra", () => {
        const dlg = document.getElementById("extraDlg");
        if (dlg) dlg.showModal();
      });
      wireBtn("btnScheduleClose", () => toggleSchedule(false));
      const reloadBtn = document.getElementById("reloadBtn");
      if (reloadBtn) reloadBtn.addEventListener("click", fetchSnap);
      const schedSeason = document.getElementById("schedSeason");
      if (schedSeason) schedSeason.addEventListener("change", () => renderSchedule(getTherm()));
      const schedTable = document.getElementById("schedTable");
      if (schedTable) schedTable.addEventListener("change", () => renderSchedule(getTherm()));

      const extraNameInp = document.getElementById("extraNameInp");
      if (extraNameInp) extraNameInp.addEventListener("input", () => { extraNameInp._dirty = true; });
      const extraNameSave = document.getElementById("extraNameSave");
      if (extraNameSave) extraNameSave.addEventListener("click", async () => {
        try {
          const v = String((extraNameInp && extraNameInp.value) ? extraNameInp.value : "").trim();
          if (!v) throw new Error("Nome vuoto");
          await sendCmd("set_description", v);
          if (extraNameInp) extraNameInp._dirty = false;
          const dlg = document.getElementById("extraDlg");
          if (dlg) dlg.close();
        } catch (e) {
          toast("Errore: " + String(e && e.message ? e.message : e));
        }
      });

      function extraProfileSeason() {
        const ent = getTherm();
        const rt = ent ? (ent.realtime || {}) : {};
        const therm = (rt.THERM && typeof rt.THERM === "object") ? rt.THERM : null;
        const sea = therm ? String(therm.ACT_SEA || "WIN").toUpperCase() : "WIN";
        return (sea === "SUM") ? "SUM" : "WIN";
      }
      function wireExtraProfile(rngId, valId, key) {
        const rng = document.getElementById(rngId);
        const out = document.getElementById(valId);
        if (!rng) return;
        const upd = () => {
          const n = Number(String(rng.value || "").replace(",", "."));
          if (out) out.textContent = Number.isFinite(n) ? (n.toFixed(1).replace(".", ",") + "\u00B0") : "--";
        };
        rng.addEventListener("input", () => { rng._dirty = true; upd(); });
        rng.addEventListener("change", async () => {
          try {
            const sea = extraProfileSeason();
            const n = Number(String(rng.value || "").replace(",", "."));
            if (!Number.isFinite(n)) return;
            pendingProfiles[String(sea) + ":" + String(key)] = { val: Number(n.toFixed(1)), ts: Date.now() };
            await sendCmd("set_profile", { season: sea, key: String(key), value: String(n.toFixed(1)) });
            rng._dirty = false;
          } catch (e) {
            toast("Errore: " + String(e && e.message ? e.message : e));
          }
        });
        upd();
      }
      wireExtraProfile("extraT1", "extraT1Val", "T1");
      wireExtraProfile("extraT2", "extraT2Val", "T2");
      wireExtraProfile("extraT3", "extraT3Val", "T3");
      wireExtraProfile("extraTM", "extraTMVal", "TM");

      // Start
      startSSE();
      setInterval(fetchSnap, 5000);
      render();
    </script>
  </body>
</html>"""

    html = (
        tpl.replace("__TITLE__", _html_escape(title))
        .replace("__TID__", tid_esc)
        .replace("__ADDON_VERSION__", _html_escape(ADDON_VERSION))
        .replace("__INIT__", init)
    )
    return html.encode("utf-8")

