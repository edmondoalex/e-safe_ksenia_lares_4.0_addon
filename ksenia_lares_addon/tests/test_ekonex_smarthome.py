import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from ekonex_smarthome import CONTRACT_PREFIX, EkonexSmartHomeBridge, set_export_entry


class FakeMqtt:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, json.loads(payload), retain))


class DoneFuture:
    def __init__(self, result):
        self.result_value = result

    def add_done_callback(self, callback):
        callback(self)

    def result(self):
        return self.result_value


class SmartHomeContractTests(unittest.TestCase):
    def setUp(self):
        self.mqtt = FakeMqtt()
        self.saved = []
        self.config = {
            "outputs": {"7": {"tag": "Luci", "visible": False}},
            "partitions": {"1": {"smart_home": {"enabled": True, "class": "switch", "capabilities": ["on"]}}},
        }
        set_export_entry(self.config, "outputs", 7, {"enabled": True, "class": "dimmer", "capabilities": ["on", "off", "level"]})
        self.snapshot = {
            "entities": [
                {"type": "outputs", "id": 7, "name": "Kitchen"},
                {"type": "partitions", "id": 1, "name": "Alarm"},
                {"type": "zones", "id": 2, "name": "Door", "realtime": {"BYP": "AUTO", "TAMPER": True}},
                {"type": "accounts", "id": 3, "name": "Admin", "static": {"PIN": "1234"}},
            ]
        }
        self.bridge = EkonexSmartHomeBridge(
            self.mqtt,
            "e-safe",
            lambda: copy.deepcopy(self.config),
            lambda value: self.saved.append(copy.deepcopy(value)),
            lambda: copy.deepcopy(self.snapshot),
            now=lambda: 123,
        )

    def test_catalog_is_whitelist_only_and_excludes_security(self):
        manifest, devices = self.bridge.build_contract()
        self.assertEqual("e-safe/status", manifest["availability_topic"])
        self.assertEqual(1, len(devices))
        self.assertEqual("outputs", devices[0]["native_type"])
        self.assertEqual("e-safe/lights/7", devices[0]["state_topic"])
        serialized = json.dumps(devices)
        for forbidden in ("partitions", "zones", "accounts", "PIN", "BYP", "TAMPER"):
            self.assertNotIn(forbidden, serialized)

    def test_device_id_survives_rename_restart_and_restore(self):
        _, first = self.bridge.build_contract()
        persisted = self.saved[-1]
        self.config = persisted
        self.snapshot["entities"][0]["name"] = "Renamed"
        restarted = EkonexSmartHomeBridge(self.mqtt, "e-safe", lambda: copy.deepcopy(self.config), lambda value: None, lambda: copy.deepcopy(self.snapshot))
        _, second = restarted.build_contract()
        self.assertEqual(first[0]["device_id"], second[0]["device_id"])

    def test_correlated_command_reuses_catalogued_legacy_topic(self):
        self.bridge.build_contract()
        raw = json.dumps({"command_id": "cmd_1", "correlation_id": "cor_1", "payload": 42})
        ctx, legacy, error = self.bridge.prepare_command("e-safe/cmd/output/7", raw)
        self.assertIsNone(error)
        self.assertEqual("42", legacy)
        self.bridge.track_future(DoneFuture(True), ctx)
        results = [m for m in self.mqtt.messages if m[0].startswith(f"{CONTRACT_PREFIX}/command_result/")]
        self.assertEqual(["accepted", "confirmed"], [m[1]["status"] for m in results])
        self.assertTrue(all(m[2] is False for m in results))

    def test_uncatalogued_and_security_commands_fail_closed(self):
        self.bridge.build_contract()
        raw = json.dumps({"command_id": "cmd_bad", "correlation_id": "cor_bad", "payload": "ARM"})
        ctx, _, error = self.bridge.prepare_command("e-safe/cmd/partition/1", raw)
        self.assertEqual("topic_not_catalogued", error)
        self.bridge.publish_result(ctx, "failed", error)
        self.assertEqual("failed", self.mqtt.messages[-1][1]["status"])

    def test_capability_and_limits_are_validated_before_dispatch(self):
        self.bridge.build_contract()
        raw = json.dumps({"command_id": "cmd_bad", "correlation_id": "cor_bad", "payload": 101})
        _, _, error = self.bridge.prepare_command("e-safe/cmd/output/7", raw)
        self.assertEqual("value_out_of_range", error)
        raw = json.dumps({"command_id": "cmd_bad2", "correlation_id": "cor_bad2", "payload": "TOGGLE"})
        _, _, error = self.bridge.prepare_command("e-safe/cmd/output/7", raw)
        self.assertEqual("capability_not_allowed", error)

    def test_visibility_and_favorite_do_not_enable_export(self):
        data = {"outputs": {"8": {"visible": True, "favorite": True}}}
        bridge = EkonexSmartHomeBridge(self.mqtt, "e-safe", lambda: data, lambda value: None, lambda: {"entities": [{"type": "outputs", "id": 8}]})
        _, devices = bridge.build_contract()
        self.assertEqual([], devices)

    def test_legacy_topic_mapping_for_all_smart_home_families(self):
        data = {}
        selections = (
            ("outputs", 10, "cover", ["open", "close", "stop", "position"]),
            ("scenarios", 11, "scenario", ["execute"]),
            ("domus", 12, "environment_sensor", ["temperature", "humidity"]),
            ("thermostats", 13, "thermostat", ["temperature", "mode", "preset"]),
        )
        for native_type, native_id, device_class, capabilities in selections:
            set_export_entry(data, native_type, native_id, {"enabled": True, "class": device_class, "capabilities": capabilities})
        snapshot = {"entities": [{"type": t, "id": i} for t, i, _, _ in selections]}
        bridge = EkonexSmartHomeBridge(self.mqtt, "e-safe", lambda: data, lambda value: None, lambda: snapshot)
        _, devices = bridge.build_contract()
        by_type = {item["native_type"]: item for item in devices}
        self.assertEqual("e-safe/cmd/cover/10", by_type["outputs"]["command_topic"])
        self.assertEqual("e-safe/cmd/scenario/11", by_type["scenarios"]["command_topic"])
        self.assertTrue(by_type["domus"]["read_only"])
        self.assertEqual("e-safe/domus/12", by_type["domus"]["state_topic"])
        self.assertEqual("e-safe/cmd/thermostat/13/temperature", by_type["thermostats"]["command_topics"]["temperature"])


if __name__ == "__main__":
    unittest.main()
