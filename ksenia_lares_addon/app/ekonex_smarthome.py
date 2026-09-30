"""Ekonex Smart Home contract layered on the existing Ksenia MQTT topics."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass


SCHEMA_VERSION = "1.0"
INTEGRATION_ID = "ksenia"
CONTRACT_PREFIX = "ekonex/v1/integrations/ksenia"

_ALLOWED_TYPES = {"outputs", "scenarios", "domus", "thermostats"}
_ALLOWED_CLASSES = {
    "outputs": {"switch", "light", "dimmer", "cover", "gate", "garage_door", "awning", "shutter"},
    "scenarios": {"scenario"},
    "domus": {"environment_sensor", "temperature_sensor", "humidity_sensor", "illuminance_sensor"},
    "thermostats": {"thermostat"},
}
_CLASS_CAPABILITIES = {
    "switch": {"on", "off", "toggle"},
    "light": {"on", "off", "toggle"},
    "dimmer": {"on", "off", "toggle", "level"},
    "cover": {"open", "close", "stop", "position"},
    "gate": {"open", "close", "stop"},
    "garage_door": {"open", "close", "stop"},
    "awning": {"open", "close", "stop", "position"},
    "shutter": {"open", "close", "stop", "position"},
    "scenario": {"execute"},
    "environment_sensor": {"temperature", "humidity", "illuminance"},
    "temperature_sensor": {"temperature"},
    "humidity_sensor": {"humidity"},
    "illuminance_sensor": {"illuminance"},
    "thermostat": {"temperature", "mode", "preset"},
}
_READ_ONLY_CLASSES = {
    "environment_sensor",
    "temperature_sensor",
    "humidity_sensor",
    "illuminance_sensor",
}


@dataclass(frozen=True)
class CommandContext:
    command_id: str
    correlation_id: str
    topic: str
    payload: str


def configure_mqtt_availability(mqtt_client, mqtt_prefix: str) -> str:
    """Configure a retained offline Last Will and return the availability topic."""
    topic = f"{str(mqtt_prefix).strip().strip('/')}/status"
    mqtt_client.will_set(topic, payload="offline", qos=1, retain=True)
    return topic


def _norm_id(value) -> str:
    try:
        return str(int(str(value).strip()))
    except Exception:
        return str(value or "").strip()


def _entity_name(entity: dict, fallback: str) -> str:
    static = entity.get("static") if isinstance(entity.get("static"), dict) else {}
    return str(static.get("DES") or static.get("NM") or entity.get("name") or fallback).strip()


def set_export_entry(data: dict, native_type: str, native_id, value: dict) -> dict:
    """Validate and persist an explicit Smart Home export selection."""
    native_type = str(native_type or "").strip().lower()
    native_id = _norm_id(native_id)
    if native_type not in _ALLOWED_TYPES or not native_id:
        raise ValueError("invalid Smart Home target")
    if not isinstance(value, dict):
        raise ValueError("smart_home must be an object")
    enabled = bool(value.get("enabled", False))
    device_class = str(value.get("class") or "").strip().lower()
    if enabled and device_class not in _ALLOWED_CLASSES[native_type]:
        raise ValueError("invalid Smart Home class")
    requested = value.get("capabilities") or []
    if not isinstance(requested, list):
        raise ValueError("capabilities must be an array")
    allowed = _CLASS_CAPABILITIES.get(device_class, set())
    capabilities = sorted({str(item).strip().lower() for item in requested if str(item).strip().lower() in allowed})
    if enabled and not capabilities:
        capabilities = sorted(allowed)
    target_map = data.setdefault(native_type, {})
    if not isinstance(target_map, dict):
        target_map = {}
        data[native_type] = target_map
    entry = target_map.get(native_id)
    if not isinstance(entry, dict):
        entry = {}
    entry["smart_home"] = {
        "enabled": enabled,
        "class": device_class,
        "capabilities": capabilities,
    }
    target_map[native_id] = entry
    return entry["smart_home"]


class EkonexSmartHomeBridge:
    def __init__(
        self,
        mqtt_client,
        mqtt_prefix,
        load_config,
        save_config,
        snapshot,
        logger=None,
        now=None,
        command_timeout=20.0,
    ):
        self.mqtt = mqtt_client
        self.mqtt_prefix = str(mqtt_prefix).strip().strip("/")
        self.load_config = load_config
        self.save_config = save_config
        self.snapshot = snapshot
        self.logger = logger
        self.now = now or time.time
        self.command_timeout = max(0.01, float(command_timeout))
        self._lock = threading.Lock()
        self._command_topics = set()
        self._topic_capabilities = {}

    def _publish_json(self, topic: str, payload: dict, retain: bool):
        self.mqtt.publish(topic, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), retain=retain)

    def _stable_id(self, data: dict, native_type: str, native_id: str) -> str:
        root = data.setdefault("ekonex_smarthome", {})
        ids = root.setdefault("device_ids", {})
        key = f"{native_type}:{native_id}"
        current = str(ids.get(key) or "").strip()
        if not current:
            current = f"ksn_{uuid.uuid4().hex}"
            ids[key] = current
        return current

    def _topics(self, native_type: str, native_id: str, device_class: str):
        prefix = self.mqtt_prefix
        if native_type == "outputs":
            if device_class in {"cover", "gate", "garage_door", "awning", "shutter"}:
                return f"{prefix}/covers/{native_id}", f"{prefix}/cmd/cover/{native_id}"
            if device_class in {"light", "dimmer"}:
                return f"{prefix}/lights/{native_id}", f"{prefix}/cmd/output/{native_id}"
            return f"{prefix}/switches/{native_id}", f"{prefix}/cmd/output/{native_id}"
        if native_type == "scenarios":
            return f"{prefix}/scenarios/{native_id}", f"{prefix}/cmd/scenario/{native_id}"
        if native_type == "domus":
            return f"{prefix}/domus/{native_id}", None
        if native_type == "thermostats":
            return f"{prefix}/thermostats/{native_id}", f"{prefix}/cmd/thermostat/{native_id}"
        return None, None

    def build_contract(self):
        with self._lock:
            data = self.load_config()
            if not isinstance(data, dict):
                data = {}
            before = json.dumps(data, sort_keys=True, ensure_ascii=False)
            snapshot = self.snapshot() or {}
            entities = snapshot.get("entities") or []
            catalog = []
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                native_type = str(entity.get("type") or "").strip().lower()
                native_id = _norm_id(entity.get("id"))
                if native_type not in _ALLOWED_TYPES or not native_id:
                    continue
                entries = data.get(native_type)
                entry = entries.get(native_id) if isinstance(entries, dict) else None
                export = entry.get("smart_home") if isinstance(entry, dict) else None
                if not isinstance(export, dict) or export.get("enabled") is not True:
                    continue
                device_class = str(export.get("class") or "").strip().lower()
                if device_class not in _ALLOWED_CLASSES[native_type]:
                    continue
                allowed = _CLASS_CAPABILITIES[device_class]
                capabilities = sorted({str(x).strip().lower() for x in (export.get("capabilities") or []) if str(x).strip().lower() in allowed})
                if not capabilities:
                    continue
                state_topic, command_topic = self._topics(native_type, native_id, device_class)
                read_only = device_class in _READ_ONLY_CLASSES
                item = {
                    "device_id": self._stable_id(data, native_type, native_id),
                    "native_type": native_type,
                    "native_id": native_id,
                    "name": _entity_name(entity, f"{native_type} {native_id}"),
                    "class": device_class,
                    "capabilities": capabilities,
                    "state_topic": state_topic,
                    "command_topic": None if read_only else command_topic,
                    "payload_format": "legacy_or_ekonex_envelope_v1" if not read_only else "ksenia_json_state",
                    "read_only": read_only,
                    "enabled": True,
                    "source": "ksenia",
                }
                if native_type == "thermostats" and not read_only:
                    item["command_topics"] = {
                        "temperature": f"{command_topic}/temperature",
                        "mode": f"{command_topic}/mode",
                        "preset": f"{command_topic}/preset_mode",
                    }
                catalog.append(item)
            if json.dumps(data, sort_keys=True, ensure_ascii=False) != before:
                self.save_config(data)
            catalog.sort(
                key=lambda x: (
                    x["native_type"],
                    0 if x["native_id"].isdigit() else 1,
                    int(x["native_id"]) if x["native_id"].isdigit() else x["native_id"],
                )
            )
            self._command_topics = {x["command_topic"] for x in catalog if x.get("command_topic")}
            self._topic_capabilities = {
                x["command_topic"]: set(x["capabilities"])
                for x in catalog
                if x.get("command_topic")
            }
            for item in catalog:
                for capability, topic in (item.get("command_topics") or {}).items():
                    self._command_topics.add(topic)
                    self._topic_capabilities[topic] = {capability}
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "integration_id": INTEGRATION_ID,
                "display_name": "Ksenia Smart Home",
                "availability_topic": f"{self.mqtt_prefix}/status",
                "mqtt_prefix": self.mqtt_prefix,
                "capabilities": sorted({cap for item in catalog for cap in item["capabilities"]}),
                "catalog_topic": f"{CONTRACT_PREFIX}/devices",
                "command_result_topic_template": f"{CONTRACT_PREFIX}/command_result/{{command_id}}",
            }
            return manifest, catalog

    def publish_contract(self):
        manifest, catalog = self.build_contract()
        self._publish_json(f"{CONTRACT_PREFIX}/manifest", manifest, True)
        self._publish_json(f"{CONTRACT_PREFIX}/devices", {"schema_version": SCHEMA_VERSION, "devices": catalog}, True)
        return len(catalog)

    def prepare_command(self, topic: str, payload_raw: str):
        try:
            payload = json.loads(payload_raw)
        except Exception:
            return None, payload_raw, None
        if not isinstance(payload, dict) or "command_id" not in payload:
            return None, payload_raw, None
        command_id = str(payload.get("command_id") or "").strip()
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if not command_id or not correlation_id:
            return None, payload_raw, "invalid_envelope"
        if topic not in self._command_topics:
            return CommandContext(command_id, correlation_id, topic, ""), payload_raw, "topic_not_catalogued"
        value = payload.get("payload", payload.get("value", payload.get("action")))
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        if value is None or str(value).strip() == "":
            return CommandContext(command_id, correlation_id, topic, ""), payload_raw, "missing_payload"
        ctx = CommandContext(command_id, correlation_id, topic, str(value))
        capability, limit_error = self._command_capability(topic, ctx.payload)
        if limit_error:
            return ctx, payload_raw, limit_error
        if capability not in self._topic_capabilities.get(topic, set()):
            return ctx, payload_raw, "capability_not_allowed"
        return ctx, ctx.payload, None

    def _command_capability(self, topic: str, value: str):
        value_upper = str(value).strip().upper()
        if "/cmd/output/" in topic:
            if value_upper in {"ON", "1", "TRUE", "T", "YES", "Y"}:
                return "on", None
            if value_upper in {"OFF", "0", "FALSE", "F", "NO", "N"}:
                return "off", None
            if value_upper == "TOGGLE":
                return "toggle", None
            raw = value_upper[2:] if value_upper.startswith(("B:", "B=")) else value_upper
            try:
                level = int(raw)
            except Exception:
                return None, "invalid_payload"
            return ("level", None) if 0 <= level <= 100 else (None, "value_out_of_range")
        if "/cmd/cover/" in topic:
            if value_upper in {"OPEN", "UP"}:
                return "open", None
            if value_upper in {"CLOSE", "DOWN"}:
                return "close", None
            if value_upper in {"STOP", "HALT"}:
                return "stop", None
            try:
                position = int(float(value_upper))
            except Exception:
                return None, "invalid_payload"
            return ("position", None) if 0 <= position <= 100 else (None, "value_out_of_range")
        if "/cmd/scenario/" in topic:
            return ("execute", None) if value_upper == "EXECUTE" else (None, "invalid_payload")
        if "/cmd/thermostat/" in topic:
            if topic.endswith("/temperature"):
                try:
                    temperature = float(str(value).replace(",", "."))
                except Exception:
                    return None, "invalid_payload"
                return ("temperature", None) if 5.0 <= temperature <= 35.0 else (None, "value_out_of_range")
            if topic.endswith("/mode"):
                return "mode", None
            if topic.endswith("/preset_mode") or topic.count("/") == 4:
                return "preset", None
        return None, "invalid_payload"

    def publish_result(
        self,
        ctx: CommandContext,
        status: str,
        error: str | None = None,
        confirmation_source: str | None = None,
    ):
        body = {
            "schema_version": SCHEMA_VERSION,
            "command_id": ctx.command_id,
            "correlation_id": ctx.correlation_id,
            "status": status,
            "timestamp": int(self.now()),
        }
        if error:
            body["error"] = str(error)
        if confirmation_source:
            body["confirmation_source"] = str(confirmation_source)
        self._publish_json(f"{CONTRACT_PREFIX}/command_result/{ctx.command_id}", body, False)

    def track_future(self, future, ctx: CommandContext | None):
        if ctx is None:
            return future
        self.publish_result(ctx, "accepted")

        completion_lock = threading.Lock()
        completion = {"done": False}

        def _finish(status, error=None, confirmation_source=None):
            with completion_lock:
                if completion["done"]:
                    return False
                completion["done"] = True
            self.publish_result(ctx, status, error, confirmation_source)
            return True

        timer = threading.Timer(
            self.command_timeout,
            lambda: _finish("timeout", "command_timeout"),
        )
        timer.daemon = True
        timer.start()

        def _done(done):
            try:
                ok = bool(done.result())
                _finish(
                    "confirmed" if ok else "failed",
                    None if ok else "native_command_rejected",
                    "native_response" if ok else None,
                )
            except TimeoutError:
                _finish("timeout", "command_timeout")
            except Exception as exc:
                _finish("failed", type(exc).__name__)
            finally:
                timer.cancel()

        future.add_done_callback(_done)
        return future
