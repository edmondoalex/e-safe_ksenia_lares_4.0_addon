import os
import json
import socket
import asyncio
import logging
import secrets
import time
import ssl
import re
import concurrent.futures
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import paho.mqtt.client as mqtt
import websockets
from websocketmanager import WebSocketManager
from debug_server import LaresState, start_debug_server, set_command_handler
from crc import addCRC
from wscall import ws_login, writeCfgTyped


def _load_addon_options():
    options_path = Path("/data/options.json")
    if not options_path.exists():
        return {}
    try:
        return json.loads(options_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Impossibile leggere {options_path}: {exc}")
        return {}


def _get_config_value(options, option_key, env_key, default):
    if env_key in os.environ and os.environ[env_key] != "":
        return os.environ[env_key]
    if option_key in options and options[option_key] not in (None, ""):
        return options[option_key]
    return default


def _get_config_int(options, option_key, env_key, default):
    value = _get_config_value(options, option_key, env_key, default)
    try:
        return int(value)
    except Exception:
        return int(default)

def _get_config_bool(options, option_key, env_key, default=False):
    value = _get_config_value(options, option_key, env_key, default)
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", ""):
        return False
    return bool(default)


def _create_mqtt_client():
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except Exception:
        return mqtt.Client()


def main():
    print("[INFO] Avvio add-on Ksenia Lares")

    options = _load_addon_options()

    ksenia_host = _get_config_value(options, "ksenia_host", "KSENIA_HOST", "")
    ksenia_port = _get_config_int(options, "ksenia_port", "KSENIA_PORT", 0)
    ksenia_pin = _get_config_value(options, "ksenia_pin", "KSENIA_PIN", "")

    mqtt_host = _get_config_value(options, "mqtt_host", "MQTT_HOST", "core-mosquitto")
    mqtt_port = _get_config_int(options, "mqtt_port", "MQTT_PORT", 1883)
    mqtt_user = _get_config_value(options, "mqtt_user", "MQTT_USER", "")
    mqtt_password = _get_config_value(options, "mqtt_password", "MQTT_PASSWORD", "")
    mqtt_prefix = _get_config_value(options, "mqtt_prefix", "MQTT_PREFIX", "ksenia")
    mqtt_debug_verbose = _get_config_bool(options, "mqtt_debug_verbose", "MQTT_DEBUG_VERBOSE", False)
    output_debug_verbose = _get_config_bool(options, "output_debug_verbose", "OUTPUT_DEBUG_VERBOSE", False)
    def _slugify_prefix(val: str) -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", str(val or "").lower()).strip("_")
        return slug or "ksenia"
    mqtt_prefix_slug = _slugify_prefix(mqtt_prefix)
    debug_thermostats = _get_config_bool(options, "debug_thermostats", "KS_DEBUG_THERMOSTATS", False)
    debug_ui_port = _get_config_int(options, "debug_ui_port", "DEBUG_UI_PORT", 8080)
    security_ui_port = _get_config_int(options, "security_ui_port", "SECURITY_UI_PORT", 8081)
    web_pin_session_required = _get_config_bool(
        options,
        "web_pin_session_required",
        "WEB_PIN_SESSION_REQUIRED",
        False,
    )
    web_pin_session_minutes_default = _get_config_int(
        options,
        "web_pin_session_minutes_default",
        "WEB_PIN_SESSION_MINUTES_DEFAULT",
        5,
    )
    icon_http_enabled = _get_config_bool(options, "icon_http_enabled", "ICON_HTTP_ENABLED", False)
    icon_http_base_url = _get_config_value(options, "icon_http_base_url", "ICON_HTTP_BASE_URL", "")
    icon_http_token = _get_config_value(options, "icon_http_token", "ICON_HTTP_TOKEN", "")
    icon_http_timeout = _get_config_int(options, "icon_http_timeout", "ICON_HTTP_TIMEOUT", 3)
    security_cmd_ws_idle_timeout_sec = _get_config_int(
        options,
        "security_cmd_ws_idle_timeout_sec",
        "SECURITY_CMD_WS_IDLE_TIMEOUT_SEC",
        20,
    )
    ws_reconnect_cooldown_sec = _get_config_int(
        options,
        "ws_reconnect_cooldown_sec",
        "WS_RECONNECT_COOLDOWN_SEC",
        8,
    )

    if not ksenia_host or not ksenia_port or not ksenia_pin:
        print("[FATAL] Config Ksenia incompleta: serve ksenia_host, ksenia_port, ksenia_pin.")
        raise SystemExit(2)

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("ksenia_lares_addon")
    if mqtt_debug_verbose:
        logger.setLevel(logging.DEBUG)
        logger.info("MQTT log verboso attivato (mqtt_debug_verbose=true).")
    if output_debug_verbose:
        logger.info("Debug output verboso attivato (output_debug_verbose=true).")

    state = LaresState()
    start_debug_server(state, port=debug_ui_port)
    start_debug_server(state, port=security_ui_port)
    logger.info("Debug UI attiva (usa 'Apri interfaccia web' dell'add-on / Ingress).")

    def _fmt_payload_for_log(payload):
        if payload is None:
            return ""
        try:
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, ensure_ascii=False)
            payload_str = str(payload)
            if len(payload_str) > 400:
                return payload_str[:400] + f"...(len={len(payload_str)})"
            return payload_str
        except Exception:
            return "<unserializable>"

    def _log_mqtt(action: str, topic: str, payload=None, retain: bool = False):
        if not mqtt_debug_verbose:
            return
        try:
            payload_str = _fmt_payload_for_log(payload)
            logger.info(f"[MQTT] {action}: topic={topic} retain={retain} payload={payload_str}")
        except Exception:
            pass

    mqttc = _create_mqtt_client()
    def _on_connect(client, userdata, flags, reason_code, properties=None):
        if mqtt_debug_verbose:
            logger.info(f"[MQTT] connesso rc={reason_code} flags={flags}")
        try:
            client.subscribe(f"{mqtt_prefix}/cmd/output/#")
            client.subscribe(f"{mqtt_prefix}/cmd/scenario/#")
            client.subscribe(f"{mqtt_prefix}/cmd/partition/#")
            client.subscribe(f"{mqtt_prefix}/cmd/zone_bypass/#")
            client.subscribe(f"{mqtt_prefix}/cmd/thermostat/#")
            client.subscribe(f"{mqtt_prefix}/cmd/scheduler/#")
            client.subscribe(f"{mqtt_prefix}/cmd/panel/#")
            client.subscribe(f"{mqtt_prefix}/cmd/account/#")
        except Exception as exc:
            logger.error(f"[MQTT] subscribe cmd/output failed: {exc}")
        try:
            # Segnala disponibilità per discovery (usato anche dagli script/scenari).
            mqttc.publish(f"{mqtt_prefix}/status", "online", retain=True)
        except Exception:
            pass

    def _on_disconnect(client, userdata, reason_code=0, properties=None):
        if mqtt_debug_verbose:
            logger.info(f"[MQTT] disconnesso rc={reason_code}")

    def _on_publish(client, userdata, mid, reason_code=None, properties=None):
        if mqtt_debug_verbose:
            logger.info(f"[MQTT] publish mid={mid} rc={reason_code}")

    manager_ref = {"manager": None, "loop": None}

    def _handle_mqtt_cmd_output(client, userdata, msg):
        try:
            topic = msg.topic or ""
            parts = topic.split("/")
            if len(parts) < 4 or parts[0] != mqtt_prefix or parts[1] != "cmd":
                return
            domain = parts[2]
            try:
                target_id = int(parts[3])
            except Exception:
                return
            sub_action = parts[4] if len(parts) >= 5 else ""
            try:
                payload_raw = msg.payload.decode("utf-8", errors="ignore") if msg.payload else ""
            except Exception:
                payload_raw = ""

            if mqtt_debug_verbose:
                try:
                    logger.info("[MQTT] cmd received: topic=%s payload=%s", topic, _fmt_payload_for_log(payload_raw))
                except Exception:
                    pass
            loop = manager_ref.get("loop")
            mgr = manager_ref.get("manager")
            if not loop or not mgr:
                return

            if domain == "output":
                p = payload_raw.strip().lower()
                action = None
                brightness = None
                if p in ("on", "1", "true", "t", "yes", "y"):
                    action = "on"
                elif p in ("off", "0", "false", "f", "no", "n"):
                    action = "off"
                elif p.startswith("b:") or p.startswith("b="):
                    try:
                        brightness = int(p[2:])
                        action = "brightness"
                    except Exception:
                        return
                elif p.isdigit():
                    try:
                        brightness = int(p)
                        action = "brightness"
                    except Exception:
                        return
                elif p == "toggle":
                    action = "toggle"
                if not action:
                    return

                async def _coro_out():
                    if action == "on":
                        ok = await mgr.turnOnOutput(target_id)
                        if output_debug_verbose:
                            logger.info("MQTT cmd/output %s -> ON ok=%s", target_id, ok)
                        if ok:
                            patch = {"ID": str(target_id), "STA": "ON"}
                            try:
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                                publish("outputs", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "off":
                        ok = await mgr.turnOffOutput(target_id)
                        if output_debug_verbose:
                            logger.info("MQTT cmd/output %s -> OFF ok=%s", target_id, ok)
                        if ok:
                            patch = {"ID": str(target_id), "STA": "OFF", "LEV": "0"}
                            try:
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                                publish("outputs", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "toggle":
                        sta_now = ""
                        try:
                            snap = state.snapshot()
                            ent = next((x for x in (snap.get("entities") or []) if x.get("type") == "outputs" and int(x.get("id") or -1) == target_id), None)
                            rt = (ent or {}).get("realtime") or {}
                            sta_now = str(rt.get("STA") or "").upper()
                        except Exception:
                            sta_now = ""
                        action_inner = "off" if sta_now == "ON" else "on"
                        if action_inner == "off":
                            ok = await mgr.turnOffOutput(target_id)
                            if output_debug_verbose:
                                logger.info("MQTT cmd/output %s -> OFF(ok toggle) ok=%s", target_id, ok)
                            if ok:
                                patch = {"ID": str(target_id), "STA": "OFF", "LEV": "0"}
                                try:
                                    state.apply_realtime_update("lights", [patch])
                                    publish("lights", patch)
                                    publish("switches", patch)
                                    publish("covers", patch)
                                    publish("outputs", patch)
                                except Exception:
                                    pass
                            return ok
                        ok = await mgr.turnOnOutput(target_id)
                        if output_debug_verbose:
                            logger.info("MQTT cmd/output %s -> ON(ok toggle) ok=%s", target_id, ok)
                        if ok:
                            patch = {"ID": str(target_id), "STA": "ON"}
                            try:
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                                publish("outputs", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "brightness":
                        try:
                            bval = max(0, min(100, int(brightness)))
                        except Exception:
                            return False
                        ok = await mgr.turnOnOutput(target_id, brightness=bval)
                        if output_debug_verbose:
                            logger.info("MQTT cmd/output %s -> brightness=%s ok=%s", target_id, bval, ok)
                        if ok:
                            patch = {"ID": str(target_id), "STA": "ON", "LEV": str(bval)}
                            try:
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                                publish("outputs", patch)
                            except Exception:
                                pass
                        return ok

                asyncio.run_coroutine_threadsafe(_coro_out(), loop)
                return

            if domain == "scenario":
                if not payload_raw.strip():
                    return
                async def _coro_scen():
                    try:
                        return await mgr.executeScenario(target_id)
                    except Exception:
                        return False
                asyncio.run_coroutine_threadsafe(_coro_scen(), loop)
                return

            if domain == "partition":
                p = payload_raw.strip().upper()
                if not p:
                    return

                # HA alarm_control_panel standard payloads
                if p in ("DISARM", "DISARMED", "OFF", "0"):
                    action = "disarm"
                elif p in ("ARM_HOME", "ARM_NIGHT", "ARM_INSTANT", "ARM_NOW", "INSTANT"):
                    action = "arm_instant"
                elif p in ("ARM_AWAY", "ARM", "ARM_DELAY", "ARMED_AWAY", "ON", "1"):
                    action = "arm"
                else:
                    return

                async def _coro_part():
                    if action == "disarm":
                        ok = await mgr.disarmPartition(target_id)
                        if ok:
                            patch = {"ID": str(target_id), "ARM": "D", "T": "0"}
                            try:
                                state.apply_realtime_update("partitions", [patch])
                                publish("partitions", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "arm_instant":
                        ok = await mgr.armPartitionInstant(target_id)
                        if ok:
                            patch = {"ID": str(target_id), "ARM": "IA", "T": "0"}
                            try:
                                state.apply_realtime_update("partitions", [patch])
                                publish("partitions", patch)
                            except Exception:
                                pass
                        return ok
                    ok = await mgr.armPartition(target_id)
                    if ok:
                        patch = {"ID": str(target_id), "ARM": "DA", "T": "0"}
                        try:
                            state.apply_realtime_update("partitions", [patch])
                            publish("partitions", patch)
                        except Exception:
                            pass
                    return ok

                asyncio.run_coroutine_threadsafe(_coro_part(), loop)
                return

            if domain == "zone_bypass":
                p = payload_raw.strip().upper()
                if not p:
                    return
                if p in ("1", "ON", "AUTO", "TRUE", "YES"):
                    action = "on"
                elif p in ("0", "OFF", "NO", "FALSE"):
                    action = "off"
                elif p in ("-1", "TGL", "TOGGLE"):
                    action = "toggle"
                else:
                    return

                async def _coro_byp():
                    ack_topic = f"{mqtt_prefix}/ack/zone_bypass/{target_id}"
                    if action == "on":
                        ok = await mgr.bypassZoneOn(target_id)
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps({"ok": bool(ok), "action": "on", "payload": p}, ensure_ascii=False),
                                retain=False,
                            )
                        except Exception:
                            pass
                        if ok:
                            patch = {"ID": str(target_id), "BYP": "AUTO"}
                            try:
                                state.apply_realtime_update("zones", [patch])
                                publish("zones", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "off":
                        ok = await mgr.bypassZoneOff(target_id)
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps({"ok": bool(ok), "action": "off", "payload": p}, ensure_ascii=False),
                                retain=False,
                            )
                        except Exception:
                            pass
                        if ok:
                            patch = {"ID": str(target_id), "BYP": "NO"}
                            try:
                                state.apply_realtime_update("zones", [patch])
                                publish("zones", patch)
                            except Exception:
                                pass
                        return ok
                    ok = await mgr.bypassZoneToggle(target_id)
                    try:
                        mqttc.publish(
                            ack_topic,
                            json.dumps({"ok": bool(ok), "action": "toggle", "payload": p}, ensure_ascii=False),
                            retain=False,
                        )
                    except Exception:
                        pass
                    if ok:
                        byp_now = ""
                        try:
                            snap = state.snapshot()
                            ent = next(
                                (
                                    x
                                    for x in (snap.get("entities") or [])
                                    if x.get("type") == "zones" and int(x.get("id") or -1) == target_id
                                ),
                                None,
                            )
                            rt = (ent or {}).get("realtime") or {}
                            byp_now = str(rt.get("BYP") or "").upper()
                        except Exception:
                            byp_now = ""
                        patch = {"ID": str(target_id), "BYP": ("NO" if byp_now in ("AUTO", "ON", "1") else "AUTO")}
                        try:
                            state.apply_realtime_update("zones", [patch])
                            publish("zones", patch)
                        except Exception:
                            pass
                    return ok

                asyncio.run_coroutine_threadsafe(_coro_byp(), loop)
                return

            if domain == "thermostat":
                action = (sub_action or "").strip().lower()
                if not action:
                    # Default: interpret as preset_mode
                    action = "preset_mode"
                p = payload_raw.strip()
                if not p:
                    return

                def _get_season_from_state(default="WIN"):
                    try:
                        snap = state.snapshot()
                        ent = next(
                            (
                                x
                                for x in (snap.get("entities") or [])
                                if x.get("type") == "thermostats" and int(x.get("id") or -1) == target_id
                            ),
                            None,
                        )
                        rt = (ent or {}).get("realtime") or {}
                        st = (ent or {}).get("static") or {}
                        therm = rt.get("THERM") if isinstance(rt, dict) else None
                        sea = None
                        if isinstance(therm, dict):
                            sea = therm.get("ACT_SEA") or therm.get("SEA")
                        if sea is None and isinstance(rt, dict):
                            sea = rt.get("ACT_SEA")
                        if sea is None and isinstance(st, dict):
                            sea = st.get("ACT_SEA")
                        sea = str(sea or "").strip().upper()
                        return sea if sea in ("WIN", "SUM") else str(default)
                    except Exception:
                        return str(default)

                def _get_target_tm_from_state(season: str) -> str | None:
                    try:
                        snap = state.snapshot()
                        ent = next(
                            (
                                x
                                for x in (snap.get("entities") or [])
                                if x.get("type") == "thermostats" and int(x.get("id") or -1) == target_id
                            ),
                            None,
                        )
                        st = (ent or {}).get("static") or {}
                        rt = (ent or {}).get("realtime") or {}
                        season = str(season or "").strip().upper()
                        for src in (rt, st):
                            if not isinstance(src, dict):
                                continue
                            sea_cfg = src.get(season)
                            if isinstance(sea_cfg, dict) and sea_cfg.get("TM") is not None:
                                return str(sea_cfg.get("TM"))
                            therm = src.get("THERM") if isinstance(src.get("THERM"), dict) else None
                            if isinstance(therm, dict):
                                sea_cfg = therm.get(season)
                                if isinstance(sea_cfg, dict) and sea_cfg.get("TM") is not None:
                                    return str(sea_cfg.get("TM"))
                        return None
                    except Exception:
                        return None

                async def _coro_therm():
                    ack_topic = f"{mqtt_prefix}/ack/thermostat/{target_id}"
                    patch = {"ID": str(target_id)}
                    if action in ("temperature", "temp", "set_temp", "set_temperature"):
                        try:
                            t = float(p.replace(",", "."))
                        except Exception:
                            try:
                                mqttc.publish(ack_topic, json.dumps({"ok": False, "action": action, "payload": p, "error": "invalid_temperature"}, ensure_ascii=False), retain=False)
                            except Exception:
                                pass
                            return False
                        t = max(5.0, min(35.0, t))
                        season = _get_season_from_state("WIN")
                        patch["ACT_MODE"] = "MAN"
                        patch["ACT_SEA"] = season
                        patch[season] = {"TM": f"{t:.1f}"}
                        ok = await mgr.updateThermostat(target_id, patch)
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps(
                                    {"ok": bool(ok), "action": action, "payload": p, "patch": patch, "ts": int(time.time())},
                                    ensure_ascii=False,
                                ),
                                retain=True,
                            )
                        except Exception:
                            pass
                        if ok:
                            try:
                                state.apply_static_update("thermostats", [patch])
                                publish("thermostats", patch)
                            except Exception:
                                pass
                        return ok

                    if action in ("mode", "hvac_mode"):
                        hv = p.strip().lower()
                        if hv in ("off", "0", "false"):
                            patch["ACT_MODE"] = "OFF"
                        elif hv in ("heat", "heating"):
                            patch["ACT_SEA"] = "WIN"
                            patch["ACT_MODE"] = "MAN"
                            tm = _get_target_tm_from_state("WIN")
                            if tm is not None:
                                patch["WIN"] = {"TM": tm}
                        elif hv in ("cool", "cooling"):
                            patch["ACT_SEA"] = "SUM"
                            patch["ACT_MODE"] = "MAN"
                            tm = _get_target_tm_from_state("SUM")
                            if tm is not None:
                                patch["SUM"] = {"TM": tm}
                        else:
                            try:
                                mqttc.publish(ack_topic, json.dumps({"ok": False, "action": action, "payload": p, "error": "invalid_hvac_mode"}, ensure_ascii=False), retain=False)
                            except Exception:
                                pass
                            return False
                        ok = await mgr.updateThermostat(target_id, patch)
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps(
                                    {"ok": bool(ok), "action": action, "payload": p, "patch": patch, "ts": int(time.time())},
                                    ensure_ascii=False,
                                ),
                                retain=True,
                            )
                        except Exception:
                            pass
                        if ok:
                            try:
                                state.apply_static_update("thermostats", [patch])
                                publish("thermostats", patch)
                            except Exception:
                                pass
                        return ok

                    if action in ("preset_mode", "preset"):
                        pm = p.strip().upper()
                        pm_map = {
                            "OFF": "OFF",
                            "MANUAL": "MAN",
                            "MAN": "MAN",
                            "SCHEDULE": "WEEKLY",
                            "WEEKLY": "WEEKLY",
                            "AUTO": "WEEKLY",
                            "MANUAL_TIMER": "MAN_TMR",
                            "MAN_TMR": "MAN_TMR",
                            "SD1": "SD1",
                            "SD2": "SD2",
                        }
                        act = pm_map.get(pm, pm)
                        patch["ACT_MODE"] = act
                        # Some panels ignore preset-only updates unless the active season target is present.
                        if act in ("MAN", "MAN_TMR"):
                            season = _get_season_from_state("WIN")
                            patch["ACT_SEA"] = season
                            tm = _get_target_tm_from_state(season)
                            if tm is not None:
                                patch[season] = {"TM": tm}
                        ok = await mgr.updateThermostat(target_id, patch)
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps(
                                    {"ok": bool(ok), "action": action, "payload": p, "patch": patch, "ts": int(time.time())},
                                    ensure_ascii=False,
                                ),
                                retain=True,
                            )
                        except Exception:
                            pass
                        if ok:
                            try:
                                state.apply_static_update("thermostats", [patch])
                                publish("thermostats", patch)
                            except Exception:
                                pass
                        return ok

                    if action in ("season", "act_sea"):
                        sea = p.strip().upper()
                        if sea not in ("WIN", "SUM"):
                            try:
                                mqttc.publish(ack_topic, json.dumps({"ok": False, "action": action, "payload": p, "error": "invalid_season"}, ensure_ascii=False), retain=False)
                            except Exception:
                                pass
                            return False
                        patch["ACT_SEA"] = sea
                        ok = await mgr.updateThermostat(target_id, patch)
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps(
                                    {"ok": bool(ok), "action": action, "payload": p, "patch": patch, "ts": int(time.time())},
                                    ensure_ascii=False,
                                ),
                                retain=True,
                            )
                        except Exception:
                            pass
                        if ok:
                            try:
                                state.apply_static_update("thermostats", [patch])
                                publish("thermostats", patch)
                            except Exception:
                                pass
                        return ok

                    try:
                        mqttc.publish(
                            ack_topic,
                            json.dumps(
                                {"ok": False, "action": action, "payload": p, "error": "unsupported_action", "ts": int(time.time())},
                                ensure_ascii=False,
                            ),
                            retain=True,
                        )
                    except Exception:
                        pass
                    return False

                asyncio.run_coroutine_threadsafe(_coro_therm(), loop)
                return

            if domain == "scheduler":
                p = payload_raw.strip().upper()
                if not p:
                    return
                if p in ("1", "ON", "TRUE", "T", "ENABLE", "ENABLED"):
                    action = "enable"
                elif p in ("0", "OFF", "FALSE", "F", "DISABLE", "DISABLED"):
                    action = "disable"
                elif p in ("-1", "TGL", "TOGGLE"):
                    action = "toggle"
                else:
                    return

                async def _coro_sched():
                    ack_topic = f"{mqtt_prefix}/ack/scheduler/{target_id}"
                    desired = None
                    if action == "toggle":
                        try:
                            snap = state.snapshot()
                            ent = next(
                                (
                                    x
                                    for x in (snap.get("entities") or [])
                                    if x.get("type") == "schedulers" and int(x.get("id") or -1) == target_id
                                ),
                                None,
                            )
                            st = (ent or {}).get("static") or {}
                            en_now = str(st.get("EN") or "").strip().upper()
                            desired = "F" if en_now in ("T", "1", "ON", "TRUE") else "T"
                        except Exception:
                            desired = "T"
                    else:
                        desired = "T" if action == "enable" else "F"

                    ok = await mgr.updateScheduler(target_id, {"ID": str(target_id), "EN": desired})
                    try:
                        mqttc.publish(
                            ack_topic,
                            json.dumps(
                                {"ok": bool(ok), "action": action, "en": desired, "ts": int(time.time())},
                                ensure_ascii=False,
                            ),
                            retain=True,
                        )
                    except Exception:
                        pass
                    if ok:
                        patch = {"ID": str(target_id), "EN": desired}
                        try:
                            state.apply_static_update("schedulers", [patch])
                            publish("schedulers", patch)
                        except Exception:
                            pass
                    return ok

                asyncio.run_coroutine_threadsafe(_coro_sched(), loop)
                return

            if domain == "panel":
                action = (sub_action or "").strip().lower()
                if not action:
                    return

                # Home Assistant MQTT button sends "payload_press".
                async def _coro_panel():
                    ack_topic = f"{mqtt_prefix}/ack/panel/{action}"
                    mapping = {
                        "clear_memories": "CYCLES_OR_MEMORIES",
                        "clear_communications": "COMMUNICATIONS",
                        "clear_faults": "FAULTS_MEMORY",
                    }
                    payload_type = mapping.get(action)
                    if not payload_type:
                        try:
                            mqttc.publish(
                                ack_topic,
                                json.dumps(
                                    {"ok": False, "action": action, "error": "unknown_action", "ts": int(time.time())},
                                    ensure_ascii=False,
                                ),
                                retain=True,
                            )
                        except Exception:
                            pass
                        return False
                    ok = await mgr.clearPanel(payload_type)
                    try:
                        mqttc.publish(
                            ack_topic,
                            json.dumps(
                                {"ok": bool(ok), "action": action, "payload_type": payload_type, "ts": int(time.time())},
                                ensure_ascii=False,
                            ),
                            retain=True,
                        )
                    except Exception:
                        pass
                    return ok

                asyncio.run_coroutine_threadsafe(_coro_panel(), loop)
                return

            if domain == "account":
                p = payload_raw.strip().upper()
                if not p:
                    return
                if p in ("1", "ON", "TRUE", "T", "ENABLE", "ENABLED"):
                    desired = "F"  # DACC=F means enabled
                elif p in ("0", "OFF", "FALSE", "F", "DISABLE", "DISABLED"):
                    desired = "T"
                else:
                    return

                async def _coro_acc():
                    ack_topic = f"{mqtt_prefix}/ack/account/{target_id}"
                    patch = {"ID": str(target_id), "DACC": desired}
                    ok = await mgr.setAccountEnabled(target_id, desired)
                    try:
                        mqttc.publish(
                            ack_topic,
                            json.dumps(
                                {"ok": bool(ok), "id": int(target_id), "dacc": desired, "ts": int(time.time())},
                                ensure_ascii=False,
                            ),
                            retain=True,
                        )
                    except Exception:
                        pass
                    if ok:
                        try:
                            state.apply_static_update("accounts", [patch])
                            publish("accounts", patch)
                        except Exception:
                            pass
                    return ok

                asyncio.run_coroutine_threadsafe(_coro_acc(), loop)
                return
        except Exception:
            if mqtt_debug_verbose:
                logger.exception("MQTT cmd/output handler error")

    mqttc.on_connect = _on_connect
    mqttc.on_disconnect = _on_disconnect
    mqttc.on_publish = _on_publish
    mqttc.on_message = _handle_mqtt_cmd_output

    if mqtt_user:
        mqttc.username_pw_set(mqtt_user, mqtt_password)
    try:
        mqttc.connect(mqtt_host, mqtt_port, 60)
    except socket.gaierror as exc:
        print(f"[FATAL] MQTT_HOST '{mqtt_host}' non risolvibile: {exc}")
        print("[FATAL] Imposta 'mqtt_host' nelle opzioni dell'add-on (o MQTT_HOST) a un hostname/IP raggiungibile.")
        print("[FATAL] Se usi Mosquitto add-on, verifica che sia installato e avviato (host tipico: core-mosquitto).")
        raise SystemExit(2)
    except Exception as exc:
        print(f"[FATAL] Connessione MQTT fallita verso {mqtt_host}:{mqtt_port}: {exc}")
        raise SystemExit(2)
    mqttc.loop_start()

    def publish(entity_type: str, item: dict):
        entity_id_raw = item.get("ID")
        if entity_id_raw is None:
            return
        # Normalize IDs so MQTT topics stay stable (e.g. avoid "033" vs "33").
        try:
            entity_id = str(int(str(entity_id_raw).strip()))
        except Exception:
            entity_id = str(entity_id_raw).strip()
        if not entity_id:
            return

        topic = f"{mqtt_prefix}/{entity_type}/{entity_id}"
        retain = entity_type not in ("logs",)
        payload = item
        try:
            # Always publish the same data you see in the Debug UI (index_debug),
            # i.e. merge static+realtime for the entity from the shared backend state.
            #
            # Note: realtime output updates are stored under entity_type="outputs" in state.
            et = str(entity_type or "").lower()
            state_entity_type = "outputs" if et in ("lights", "switches", "covers", "outputs") else entity_type
            merged = state.get_merged(state_entity_type, entity_id)
            if isinstance(merged, dict) and merged:
                payload = dict(merged)
                if "ID" not in payload:
                    payload["ID"] = entity_id
                else:
                    # Keep ID consistent with topic/discovery.
                    try:
                        payload["ID"] = str(int(str(payload.get("ID")).strip()))
                    except Exception:
                        payload["ID"] = entity_id
        except Exception:
            payload = item

        _log_mqtt("publish", topic, payload, retain)
        mqttc.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)
        # Mirror state to discovery state_topic (homeassistant/...) so HA picks up changes.
        try:
            et = str(entity_type).lower()
            if et == "zones":
                sta = str(payload.get("STA") or "").upper()
                _log_mqtt("publish", f"{DISC_PREFIX}/binary_sensor/{mqtt_prefix_slug}_zone_{entity_id}/state", sta, True)
                mqttc.publish(f"{DISC_PREFIX}/binary_sensor/{mqtt_prefix_slug}_zone_{entity_id}/state", sta, retain=True)
            elif et == "partitions":
                arm_raw = payload.get("ARM")
                if isinstance(arm_raw, dict):
                    arm = str(arm_raw.get("S") or arm_raw.get("s") or arm_raw.get("CODE") or "").upper()
                else:
                    arm = str(arm_raw or "").upper()

                if arm in ("D", "DISARM", "DISINSERITO"):
                    ha_state = "disarmed"
                elif arm == "A" or arm.startswith("A_") or arm in ("AL", "ALARM"):
                    ha_state = "triggered"
                elif arm.startswith("I"):
                    ha_state = "armed_home"
                else:
                    ha_state = "armed_away"

                obj_id = f"{mqtt_prefix_slug}_part_{entity_id}"
                _log_mqtt("publish", f"{DISC_PREFIX}/alarm_control_panel/{obj_id}/state", ha_state, True)
                mqttc.publish(f"{DISC_PREFIX}/alarm_control_panel/{obj_id}/state", ha_state, retain=True)
            elif et == "thermostats":
                # Publish derived HA-friendly thermostat state topics (retained) to avoid
                # template issues when payloads are partial or nested.
                try:
                    import datetime as _dt
                    act_sea = str(payload.get("ACT_SEA") or "").strip().upper()
                    act_mode = str(payload.get("ACT_MODE") or "").strip().upper()
                    therm = payload.get("THERM") if isinstance(payload.get("THERM"), dict) else None
                    if therm:
                        act_sea = str(therm.get("ACT_SEA") or act_sea or "").strip().upper()
                        act_mode = str(therm.get("ACT_MODE") or act_mode or "").strip().upper()

                    if act_sea not in ("WIN", "SUM"):
                        act_sea = "WIN"
                    if not act_mode:
                        act_mode = "OFF"

                    off_vals = {"OFF", "0", "FALSE", "NO", "N"}
                    if act_mode in off_vals:
                        hvac_mode = "off"
                    else:
                        hvac_mode = "cool" if act_sea == "SUM" else "heat"

                    m = act_mode
                    if m in off_vals:
                        preset = "off"
                    elif ("WEEK" in m) or (m == "AUTO"):
                        preset = "schedule"
                    elif ("TMR" in m):
                        preset = "manual_timer"
                    elif m in ("SD1", "SD2"):
                        preset = m.lower()
                    elif m.startswith("MAN"):
                        preset = "manual"
                    else:
                        preset = "manual"

                    cur_raw = None
                    temp = payload.get("TEMP")
                    if isinstance(temp, dict):
                        cur_raw = temp.get("IN")
                    if cur_raw is None:
                        cur_raw = payload.get("TEMP")
                    try:
                        cur = float(str(cur_raw).replace(",", "."))
                    except Exception:
                        cur = None

                    cfg = payload.get(act_sea)
                    if not isinstance(cfg, dict) and therm and isinstance(therm.get(act_sea), dict):
                        cfg = therm.get(act_sea)

                    def _effective_target(cfg_dict: dict, mode: str, season: str):
                        if not isinstance(cfg_dict, dict):
                            return None
                        mode = str(mode or "").upper()
                        season = str(season or "").upper()
                        # Manual target (fallback)
                        tm = cfg_dict.get("TM")

                        # Scheduled target: map current hour slot T -> T1/T2/T3 temperature.
                        if mode in ("WEEKLY", "AUTO", "SD1", "SD2"):
                            now = _dt.datetime.now()
                            hour = int(now.hour)
                            dow = now.isoweekday()  # 1=Mon..7=Sun
                            day_keys = {1: "MON", 2: "TUE", 3: "WED", 4: "THU", 5: "FRI", 6: "SAT", 7: "SUN"}
                            day = day_keys.get(dow, "MON")
                            if mode in ("SD1", "SD2"):
                                day = mode
                            sched = cfg_dict.get(day)
                            if isinstance(sched, list) and hour < len(sched):
                                slot = sched[hour]
                                if isinstance(slot, dict):
                                    tcode = str(slot.get("T") or "").strip()
                                    if tcode in ("1", "2", "3"):
                                        tkey = f"T{tcode}"
                                        if cfg_dict.get(tkey) is not None:
                                            return cfg_dict.get(tkey)
                        return tm

                    tgt_raw = _effective_target(cfg, act_mode, act_sea) if isinstance(cfg, dict) else None
                    try:
                        tgt = float(str(tgt_raw).replace(",", "."))
                    except Exception:
                        tgt = None

                    if hvac_mode == "off":
                        action = "off"
                    elif hvac_mode == "cool":
                        if (cur is not None) and (tgt is not None) and (cur > tgt + 0.1):
                            action = "cooling"
                        else:
                            action = "idle"
                    else:
                        if (cur is not None) and (tgt is not None) and (cur + 0.1 < tgt):
                            action = "heating"
                        else:
                            action = "idle"

                    base = f"{mqtt_prefix}/thermostats/{entity_id}"
                    for suffix, val in (
                        ("hvac_mode", hvac_mode),
                        ("preset_mode", preset),
                        ("action", action),
                        ("target_temperature", ("" if tgt is None else f"{tgt:.1f}")),
                        ("current_temperature", ("" if cur is None else f"{cur:.1f}")),
                    ):
                        tpc = f"{base}/{suffix}"
                        _log_mqtt("publish", tpc, val, True)
                        mqttc.publish(tpc, str(val), retain=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _partition_arm_state(part_payload: dict) -> str:
        if not isinstance(part_payload, dict):
            return ""
        arm_raw = part_payload.get("ARM")
        if isinstance(arm_raw, dict):
            return str(arm_raw.get("S") or arm_raw.get("s") or arm_raw.get("CODE") or "").strip().upper()
        return str(arm_raw or "").strip().upper()

    def _partition_is_disarmed(part_payload: dict) -> bool:
        s = _partition_arm_state(part_payload)
        # Some panels use codes like DA/DT/etc for armed/transition states; only treat plain 'D'
        # (and explicit strings) as disarmed for clearing derived sensors.
        return (s == "") or (s == "D") or (s in ("DISARM", "DISINSERITO"))

    def _partition_ids_from_state() -> list[int]:
        try:
            snap = state.snapshot()
            entities = snap.get("entities") or []
        except Exception:
            entities = []
        out: list[int] = []
        if not isinstance(entities, list):
            return out
        for e in entities:
            if not isinstance(e, dict):
                continue
            if str(e.get("type") or "").lower() != "partitions":
                continue
            try:
                out.append(int(str(e.get("id")).strip()))
            except Exception:
                continue
        return sorted(set([x for x in out if x > 0]))

    def _decode_zone_prt_partition_ids(prt_value, partition_ids: list[int]) -> list[int]:
        """
        Decode a zone->partition association from the static PRT field.

        Panels/UI vary:
        - bitmask where bit position == partition ID (1-based)
        - bitmask where bit position maps to the *nth enabled partition* (compact)
        - string lists like "1,2"
        - hex strings without 0x ("40")
        """
        if prt_value is None:
            return []
        s = str(prt_value).strip()
        if not s:
            return []
        up = s.upper()
        if up in ("ALL", "ALLALL"):
            return list(partition_ids)

        # explicit lists like "1,2"
        if "," in s or " " in s:
            out = []
            for tok in re.split(r"[,\s]+", s):
                tok = str(tok or "").strip()
                if not tok:
                    continue
                try:
                    pid = int(tok)
                except Exception:
                    continue
                if pid > 0:
                    out.append(pid)
            return sorted(set(out))

        # numeric/hex mask candidates
        candidates: list[int] = []
        try:
            candidates.append(int(float(s)))
        except Exception:
            pass
        is_hexish = all(c in "0123456789abcdefABCDEF" for c in s) and len(s) >= 2
        try:
            if up.startswith("0X"):
                candidates.append(int(s, 16))
            elif is_hexish:
                candidates.append(int(s, 16))
        except Exception:
            pass
        if not candidates:
            return []

        # Decode mask by two strategies and pick best:
        # - direct: bit i -> partition ID i
        # - compact: bit i -> partition_ids[i-1]
        max_bit_direct = max(partition_ids) if partition_ids else 32
        max_bit_direct = max(max_bit_direct, 32)
        max_bit_compact = max(len(partition_ids), 1)
        max_bit_compact = max(max_bit_compact, 32)

        def _decode_bits(mask_int: int, max_bit: int) -> list[int]:
            out = []
            for i in range(1, max_bit + 1):
                if mask_int & (1 << (i - 1)):
                    out.append(i)
            return out

        best_ids: list[int] = []
        best_score = None
        part_set = set(partition_ids)
        for m in candidates:
            if m < 0:
                continue
            direct = _decode_bits(m, max_bit_direct)
            compact_bits = _decode_bits(m, max_bit_compact)
            compact = []
            for idx in compact_bits:
                if 1 <= idx <= len(partition_ids):
                    compact.append(partition_ids[idx - 1])

            for mode, ids in (("direct", direct), ("compact", compact)):
                if not ids:
                    continue
                # score: prefer ids that exist as partitions, and fewer ids overall.
                nonexist = sum(1 for x in ids if x not in part_set) if part_set else 0
                exist = sum(1 for x in ids if x in part_set) if part_set else len(ids)
                score = (nonexist, -exist, len(ids), 0 if mode == "compact" else 1)
                if best_score is None or score < best_score:
                    best_score = score
                    best_ids = ids

        return sorted(set([x for x in best_ids if x > 0]))

    def _zone_is_alarm(zone_payload: dict) -> bool:
        if not isinstance(zone_payload, dict):
            return False
        rt = zone_payload.get("realtime") if isinstance(zone_payload.get("realtime"), dict) else {}

        def _n(v):
            return str(v if v is not None else "").strip().upper()

        sta = _n(rt.get("STA"))
        a = _n(rt.get("A"))
        if sta in ("A", "AL", "ALARM", "TRIG", "TRIGGERED"):
            return True
        if a not in ("", "N", "0", "F", "OFF", "FALSE", "NO"):
            return True
        return False

    def _parse_zone_id(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, dict):
            cand = value.get("ID") or value.get("id") or value.get("ZONE") or value.get("zone")
            return _parse_zone_id(cand)
        s = str(value).strip()
        if not s:
            return None
        # Accept "33", "Z33", "ZONE_33", etc.
        m = re.search(r"(\d+)", s)
        if not m:
            return None
        try:
            n = int(m.group(1))
        except Exception:
            return None
        return n if n > 0 else None

    def _system_alarm_zone_ids() -> set[int]:
        alarm_ids: set[int] = set()
        try:
            snap = state.snapshot()
            entities = snap.get("entities") or []
        except Exception:
            entities = []
        if not isinstance(entities, list):
            return alarm_ids
        for e in entities:
            if not isinstance(e, dict):
                continue
            if str(e.get("type") or "").lower() != "systems":
                continue
            sid = str(e.get("id") or "").strip()
            try:
                sys_merged = state.get_merged("systems", sid) if sid else e
            except Exception:
                sys_merged = e
            if not isinstance(sys_merged, dict):
                continue
            rt = sys_merged.get("realtime") if isinstance(sys_merged.get("realtime"), dict) else {}
            # Prefer current alarms list; if empty, try ALARM_MEM (some panels only expose memory).
            alarm_list = rt.get("ALARM")
            if not isinstance(alarm_list, list) or not alarm_list:
                alarm_list = rt.get("ALARM_MEM")
            if not isinstance(alarm_list, list):
                continue
            for it in alarm_list:
                zid = _parse_zone_id(it)
                if zid:
                    alarm_ids.add(int(zid))
        return alarm_ids

    def _zones_alarm_for_partition(pid: int) -> list[tuple[str, str]]:
        if pid <= 0:
            return []
        out: list[tuple[str, str]] = []
        alarm_ids = _system_alarm_zone_ids()
        partition_ids = _partition_ids_from_state()
        try:
            snap = state.snapshot()
            entities = snap.get("entities") or []
        except Exception:
            entities = []
        if not isinstance(entities, list):
            return []
        for e in entities:
            if not isinstance(e, dict):
                continue
            if str(e.get("type") or "").lower() != "zones":
                continue
            zid = str(e.get("id") or "").strip()
            if not zid:
                continue
            zid_int = None
            try:
                zid_int = int(str(zid).strip())
            except Exception:
                zid_int = _parse_zone_id(zid)
            if alarm_ids and (not zid_int or int(zid_int) not in alarm_ids):
                continue
            try:
                merged = state.get_merged("zones", zid)
            except Exception:
                merged = e
            if not isinstance(merged, dict):
                continue
            st = merged.get("static") if isinstance(merged.get("static"), dict) else {}
            pids = _decode_zone_prt_partition_ids(st.get("PRT"), partition_ids)
            if pid not in pids:
                continue
            # If systems provides ALARM list, trust it; otherwise fallback to per-zone heuristic.
            if (not alarm_ids) and (not _zone_is_alarm(merged)):
                continue
            name = str(merged.get("name") or "").strip() or f"Zona {zid}"
            out.append((zid, name))
        return out

    def _format_alarm_zones_state(items: list[tuple[str, str]]) -> str:
        if not items:
            return "Nessuno"
        parts = [str(nm).strip() for _zid, nm in items if str(nm or "").strip()]
        s = ", ".join(parts)
        if not s:
            return "Nessuno"
        if len(s) <= 240:
            return s
        return s[:235].rstrip() + "…"

    # Active alarm zone derived from LOGS (ZALARM), per partition.
    # key: partition_id -> display_name (last alarmed zone)
    _alarm_zone_last: dict[int, str] = {}

    def _norm_text(s: str) -> str:
        return re.sub(r"\s+", " ", str(s or "").strip()).casefold()

    def _find_zone_by_name(zone_name: str) -> tuple[str | None, str | None]:
        """
        Resolve a log zone description (I1) to (zone_id, display_name) by matching zone static.DES/name.
        """
        target = _norm_text(zone_name)
        if not target:
            return None, None
        try:
            snap = state.snapshot()
            entities = snap.get("entities") or []
        except Exception:
            entities = []
        if not isinstance(entities, list):
            return None, None
        for e in entities:
            if not isinstance(e, dict):
                continue
            if str(e.get("type") or "").lower() != "zones":
                continue
            zid = str(e.get("id") or "").strip()
            if not zid:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            cand = st.get("DES") or e.get("name") or ""
            if _norm_text(cand) == target:
                return zid, (str(cand).strip() or f"Zona {zid}")
        return None, None

    def _armed_partition_ids() -> list[int]:
        try:
            snap = state.snapshot()
            entities = snap.get("entities") or []
        except Exception:
            entities = []
        out: list[int] = []
        if not isinstance(entities, list):
            return out
        for e in entities:
            if not isinstance(e, dict):
                continue
            if str(e.get("type") or "").lower() != "partitions":
                continue
            pid_s = str(e.get("id") or "").strip()
            try:
                pid = int(pid_s)
            except Exception:
                continue
            try:
                merged = state.get_merged("partitions", pid_s)
            except Exception:
                merged = e
            if not isinstance(merged, dict):
                continue
            if not _partition_is_disarmed(merged):
                out.append(pid)
        return sorted(set([p for p in out if p > 0]))

    def _set_alarm_zone_for_partitions(zone_name: str, pids: list[int]):
        disp = str(zone_name).strip()
        if not disp:
            return
        for pid in pids:
            if pid <= 0:
                continue
            _alarm_zone_last[int(pid)] = disp

    def _active_alarm_zone_for_partition(pid: int) -> str:
        return str(_alarm_zone_last.get(int(pid)) or "").strip()

    def publish_alarm_zones_for_partition(pid: int):
        try:
            pid_int = int(pid)
        except Exception:
            return
        if pid_int <= 0:
            return
        try:
            part = state.get_merged("partitions", str(pid_int))
        except Exception:
            part = None
        if isinstance(part, dict) and _partition_is_disarmed(part):
            state_str = "Nessuno"
        else:
            active = _active_alarm_zone_for_partition(pid_int)
            if active:
                state_str = active
            else:
                # Fallback to computed realtime heuristics: pick the first matching zone name.
                computed = _zones_alarm_for_partition(pid_int)
                state_str = str(computed[0][1]).strip() if computed else "Nessuno"
        topic = f"{mqtt_prefix}/partitions/{pid_int}/alarm_zones"
        if mqtt_debug_verbose:
            try:
                logger.info("alarm_zones p%s => %s", pid_int, state_str)
            except Exception:
                pass
        _log_mqtt("publish", topic, state_str, True)
        mqttc.publish(topic, state_str, retain=True)

    def publish_alarm_zones_for_all_partitions():
        try:
            snap = state.snapshot()
            entities = snap.get("entities") or []
        except Exception:
            entities = []
        if not isinstance(entities, list):
            return
        pids: list[int] = []
        for e in entities:
            if not isinstance(e, dict):
                continue
            if str(e.get("type") or "").lower() != "partitions":
                continue
            try:
                pids.append(int(str(e.get("id")).strip()))
            except Exception:
                continue
        for pid in sorted(set([p for p in pids if p > 0])):
            publish_alarm_zones_for_partition(pid)

    # ------------------------------------------------------------------
    # MQTT Discovery (read-only: espone stato via Home Assistant MQTT)
    # ------------------------------------------------------------------
    DISC_PREFIX = "homeassistant"

    def _disc_device(snapshot: dict, group_key: str, group_name: str) -> dict:
        try:
            host_slug = re.sub(r"[^a-z0-9]+", "_", str(ksenia_host or "").lower()).strip("_") or "panel"
        except Exception:
            host_slug = "panel"
        group_key = re.sub(r"[^a-z0-9_]+", "_", str(group_key or "").lower()).strip("_") or "main"
        identifier = f"{mqtt_prefix_slug}_lares_{host_slug}_{group_key}"
        device = {
            "identifiers": [identifier],
            "name": f"{mqtt_prefix} {group_name}".strip(),
            "manufacturer": "Ksenia / Ekonex",
            "model": "Lares",
        }
        try:
            sv = (snapshot or {}).get("system_version") or {}
            if isinstance(sv, dict):
                fw = sv.get("FW") or sv.get("fw") or sv.get("firmware")
                if fw:
                    device["sw_version"] = str(fw)
        except Exception:
            pass
        return device

    def _disc_publish(domain: str, object_id: str, payload: dict) -> bool:
        if not object_id:
            return False
        try:
            topic = f"{DISC_PREFIX}/{domain}/{object_id}/config"
            _log_mqtt("discovery", topic, payload, True)
            mqttc.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)
            return True
        except Exception as exc:
            logger.error(f"Discovery publish failed for {domain} {object_id}: {exc}")
            return False

    def publish_discovery(snapshot: dict):
        entities = snapshot.get("entities") or []
        published = 0
        per_type = {}
        disc_devices = {
            "zones": _disc_device(snapshot, "zones", "Sensori"),
            "partitions": _disc_device(snapshot, "partitions", "Partizioni"),
            "outputs": _disc_device(snapshot, "outputs", "Uscite"),
            "scenarios": _disc_device(snapshot, "scenarios", "Scenari"),
            "thermostats": _disc_device(snapshot, "thermostats", "Termostati"),
            "schedulers": _disc_device(snapshot, "schedulers", "Programmatori"),
            "panel": _disc_device(snapshot, "panel", "Reset/Rapidi"),
            "accounts": _disc_device(snapshot, "accounts", "Utenti"),
            "systems": _disc_device(snapshot, "systems", "Sistema"),
        }

        def _apply_device(payload: dict, group: str) -> dict:
            if not isinstance(payload, dict) or "device" in payload:
                return payload
            dev = disc_devices.get(group)
            if dev:
                payload["device"] = dev
            return payload

        def _inc(t):
            if not t:
                return
            per_type[t] = per_type.get(t, 0) + 1

        # Zones -> binary_sensor
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "zones":
                continue
            _inc(et)
            try:
                eid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Zona {eid}"
            rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
            cat = str((st.get("CAT") or rt.get("CAT") or "")).upper()
            zone_icon = None
            zone_device_class = "safety"
            name_up = str(name or "").upper()
            is_motion = ("INT" in cat) or ("PIR" in name_up) or (" IR " in f" {name_up} ") or name_up.startswith("IR ")
            if "PER" in cat:
                zone_icon = "mdi:door-closed"
            elif is_motion:
                # Movement zone: let HA pick proper ON/OFF icon automatically.
                zone_device_class = "motion"
                zone_icon = None
            elif ("24" in cat) or ("H24" in cat):
                zone_icon = "mdi:shield"
            obj_id = f"{mqtt_prefix_slug}_zone_{eid}"
            state_topic = f"{DISC_PREFIX}/binary_sensor/{mqtt_prefix_slug}_zone_{eid}/state"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "state_topic": state_topic,
                "device_class": zone_device_class,
                "payload_on": "A",
                "payload_off": "R",
                "default_entity_id": f"binary_sensor.{obj_id}",
            }
            payload = _apply_device(payload, "zones")
            if zone_icon:
                payload["icon"] = zone_icon
            if _disc_publish("binary_sensor", obj_id, payload):
                published += 1

            # Extra zone sensors (read from JSON state) for alarm/bypass/tamper/mask.
            zone_json_topic = f"{mqtt_prefix}/zones/{eid}"
            extra = [
                (
                    "alarm",
                    "IN Allarme",
                    "safety",
                    "{{ 'ON' if (value_json.A | default('N') | upper) not in ['N','0','F','OFF','FALSE','NO',''] else 'OFF' }}",
                    "mdi:alarm-bell",
                ),
                (
                    "bypass",
                    "Bypass",
                    "problem",
                    "{{ 'ON' if (value_json.BYP | default('NO') | upper) not in ['NO','N','0','OFF','FALSE',''] else 'OFF' }}",
                    "mdi:shield-off",
                ),
                (
                    "tamper",
                    "Sabotaggio",
                    "tamper",
                    "{{ 'ON' if ( (value_json.T | default('N') | upper) not in ['N','0','F','OFF','FALSE','NO','', 'NA'] ) or ( (value_json.AN | default('F') | upper) in ['T','Y','1','ON','TRUE'] ) or ( (value_json.STA | default('') | upper) in ['T','S','SAB','TAMPER'] ) else 'OFF' }}",
                    "mdi:shield-alert",
                ),
                (
                    "mask",
                    "Mascheramento",
                    "problem",
                    "{{ 'ON' if ((value_json.FM | default('F') | upper) in ['T','Y','1','ON','TRUE']) or ((value_json.VAS | default('F') | upper) in ['T','Y','1','ON','TRUE']) else 'OFF' }}",
                    "mdi:eye-off",
                ),
            ]
            for suffix, label, dev_class, tmpl, icon in extra:
                obj_id2 = f"{mqtt_prefix_slug}_zone_{eid}_{suffix}"
                payload2 = {
                    "name": f"{name} {label}",
                    "unique_id": obj_id2,
                    "state_topic": zone_json_topic,
                    "value_template": tmpl,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device_class": dev_class,
                    "icon": icon,
                    "default_entity_id": f"binary_sensor.{obj_id2}",
                }
                payload2 = _apply_device(payload2, "zones")
                if _disc_publish("binary_sensor", obj_id2, payload2):
                    published += 1

            # Zone bypass control (r/w): command BYP via MQTT and read back from zone JSON.
            obj_id3 = f"{mqtt_prefix_slug}_zone_{eid}_bypass_ctrl"
            payload3 = {
                "name": f"{name} Bypass (Comando)",
                "unique_id": obj_id3,
                "state_topic": zone_json_topic,
                "value_template": "{{ 'ON' if (value_json.BYP | default('NO') | upper) not in ['NO','N','0','OFF','FALSE',''] else 'OFF' }}",
                "command_topic": f"{mqtt_prefix}/cmd/zone_bypass/{eid}",
                "payload_on": "AUTO",
                "payload_off": "NO",
                "state_on": "ON",
                "state_off": "OFF",
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"switch.{obj_id3}",
                "icon": "mdi:cancel",
            }
            payload3 = _apply_device(payload3, "zones")
            if _disc_publish("switch", obj_id3, payload3):
                published += 1

        # Partitions -> alarm_control_panel (r/w)
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "partitions":
                continue
            _inc(et)
            try:
                eid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Partizione {eid}"
            obj_id = f"{mqtt_prefix_slug}_part_{eid}"
            state_topic = f"{DISC_PREFIX}/alarm_control_panel/{obj_id}/state"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "state_topic": state_topic,
                "command_topic": f"{mqtt_prefix}/cmd/partition/{eid}",
                "payload_disarm": "DISARM",
                "payload_arm_away": "ARM_AWAY",
                "payload_arm_home": "ARM_HOME",
                "payload_arm_night": "ARM_NIGHT",
                # Avoid HA requiring a code for arming/disarming.
                "code_arm_required": False,
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"alarm_control_panel.{obj_id}",
            }
            payload = _apply_device(payload, "partitions")
            if _disc_publish("alarm_control_panel", obj_id, payload):
                published += 1

            # Partitions -> binary_sensor (armed/disarmed, ignore delay/immediate)
            try:
                obj_id2 = f"{mqtt_prefix_slug}_part_{eid}_armed"
                payload2 = {
                    "name": f"Stato partizioni {name}",
                    "unique_id": obj_id2,
                    "state_topic": f"{mqtt_prefix}/partitions/{eid}",
                    "value_template": "{% set a = value_json.get('ARM') %}{% if a is mapping %}{% set s = (a.get('S') or '') %}{% else %}{% set s = (a or '') %}{% endif %}{% set s = (s|string)|upper %}{{ 'ON' if s not in ['','D','DISARM','DISINSERITO'] else 'OFF' }}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device_class": "safety",
                    "availability_topic": f"{mqtt_prefix}/status",
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "default_entity_id": f"binary_sensor.{mqtt_prefix_slug}_part_{eid}_armed",
                }
                payload2 = _apply_device(payload2, "partitions")
                if _disc_publish("binary_sensor", obj_id2, payload2):
                    published += 1
            except Exception:
                pass

            # Partition alarm zones summary -> sensor (text)
            try:
                part_name = str(name or f"Partizione {eid}").strip() or f"Partizione {eid}"
                obj_id2 = f"{mqtt_prefix_slug}_part_{eid}_alarm_zones"
                payload2 = {
                    "name": f"Stato sensori in allarme ({part_name})",
                    "unique_id": obj_id2,
                    "state_topic": f"{mqtt_prefix}/partitions/{eid}/alarm_zones",
                    "icon": "mdi:alarm-light",
                    "default_entity_id": f"sensor.{mqtt_prefix_slug}_part_{eid}_sensori_in_allarme",
                    "availability_topic": f"{mqtt_prefix}/status",
                    "payload_available": "online",
                    "payload_not_available": "offline",
                }
                payload2 = _apply_device(payload2, "partitions")
                if _disc_publish("sensor", obj_id2, payload2):
                    published += 1
            except Exception:
                pass

        # Outputs -> switch (controllo via MQTT)
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "outputs":
                continue
            _inc(et)
            try:
                eid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Uscita {eid}"
            obj_id = f"{mqtt_prefix_slug}_out_{eid}"
            state_topic = f"{mqtt_prefix}/outputs/{eid}"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "state_topic": state_topic,
                "command_topic": f"{mqtt_prefix}/cmd/output/{eid}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                # Accept both raw payloads ({STA:...}) and merged payloads ({realtime:{STA:...}}).
                "value_template": "{{ 'ON' if ((value_json.STA | default(value_json.realtime.STA | default('')) ) | upper) == 'ON' else 'OFF' }}",
                "default_entity_id": f"switch.{obj_id}",
            }
            payload = _apply_device(payload, "outputs")
            if _disc_publish("switch", obj_id, payload):
                published += 1

        # Scenari -> button (solo chiamata)
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "scenarios":
                continue
            _inc(et)
            try:
                sid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Scenario {sid}"
            obj_id = f"{mqtt_prefix_slug}_scen_{sid}"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "command_topic": f"{mqtt_prefix}/cmd/scenario/{sid}",
                "payload_press": "EXECUTE",
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"button.{obj_id}",
            }
            payload = _apply_device(payload, "scenarios")
            if _disc_publish("button", obj_id, payload):
                published += 1

        # Thermostats -> climate (r/w via MQTT)
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "thermostats":
                continue
            _inc(et)
            try:
                tid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Termostato {tid}"
            obj_id = f"{mqtt_prefix_slug}_therm_{tid}"
            state_topic = f"{mqtt_prefix}/thermostats/{tid}"
            base_topic = f"{mqtt_prefix}/thermostats/{tid}"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"climate.{obj_id}",
                # HVAC mode: map Ksenia ACT_MODE/ACT_SEA to HA modes (avoid invalid 'MAN').
                "mode_state_topic": f"{base_topic}/hvac_mode",
                "mode_command_topic": f"{mqtt_prefix}/cmd/thermostat/{tid}/mode",
                "modes": ["off", "heat", "cool"],
                # Preset mode: expose Ksenia ACT_MODE (MAN/WEEKLY/SD1/SD2/...).
                "preset_mode_state_topic": f"{base_topic}/preset_mode",
                "preset_mode_command_topic": f"{mqtt_prefix}/cmd/thermostat/{tid}/preset_mode",
                "preset_mode_command_template": "{% set p = value | default('') | lower %}{% if p == 'off' %}OFF{% elif p in ['schedule','weekly','auto'] %}WEEKLY{% elif p in ['manual_timer','man_tmr'] %}MAN_TMR{% elif p in ['manual','man'] %}MAN{% elif p in ['sd1','sd2'] %}{{ p | upper }}{% else %}{{ value }}{% endif %}",
                "preset_modes": ["off", "manual", "schedule", "manual_timer", "sd1", "sd2"],
                # Temperatures: target + current.
                "temperature_state_topic": f"{base_topic}/target_temperature",
                "temperature_command_topic": f"{mqtt_prefix}/cmd/thermostat/{tid}/temperature",
                "current_temperature_topic": f"{base_topic}/current_temperature",
                # Heating/cooling activity in HA.
                "action_topic": f"{base_topic}/action",
                "precision": 0.1,
                "temp_step": 0.1,
                "min_temp": 5,
                "max_temp": 35,
                "supported_features": 1,
            }
            payload = _apply_device(payload, "thermostats")
            if _disc_publish("climate", obj_id, payload):
                published += 1

        # Scheduler timers -> switch (enable/disable)
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "schedulers":
                continue
            _inc(et)
            try:
                sid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Timer {sid}"
            obj_id = f"{mqtt_prefix_slug}_sched_{sid}"
            state_topic = f"{mqtt_prefix}/schedulers/{sid}"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "state_topic": state_topic,
                "value_template": "{{ 'ON' if (value_json.EN | default('F') | upper) in ['T','1','ON','TRUE','YES'] else 'OFF' }}",
                "command_topic": f"{mqtt_prefix}/cmd/scheduler/{sid}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"switch.{obj_id}",
                "icon": "mdi:clock-outline",
            }
            payload = _apply_device(payload, "schedulers")
            if _disc_publish("switch", obj_id, payload):
                published += 1

        # Panel quick actions -> button
        for action, name, icon in (
            ("clear_memories", "Reset Memorie", "mdi:clipboard-check-outline"),
            ("clear_communications", "Reset Comunicazioni", "mdi:lan-disconnect"),
            ("clear_faults", "Reset Guasti", "mdi:alert-remove-outline"),
        ):
            obj_id = f"{mqtt_prefix_slug}_panel_{action}"
            payload = {
                "name": name,
                "unique_id": obj_id,
                "command_topic": f"{mqtt_prefix}/cmd/panel/1/{action}",
                "payload_press": "PRESS",
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"button.{obj_id}",
                "icon": icon,
            }
            payload = _apply_device(payload, "panel")
            if _disc_publish("button", obj_id, payload):
                published += 1

        # Accounts -> switch (enable/disable users)
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "accounts":
                continue
            _inc(et)
            try:
                aid = str(int(e.get("id")))
            except Exception:
                continue
            st = e.get("static") if isinstance(e.get("static"), dict) else {}
            name = st.get("DES") or e.get("name") or f"Account {aid}"
            # Use a dedicated unique_id/object_id for the user switch to avoid conflicts
            # with legacy binary_sensor unique_ids (e.g. e_safe_acc_6).
            obj_id = f"{mqtt_prefix_slug}_user_{aid}"
            state_topic = f"{mqtt_prefix}/accounts/{aid}"
            payload = {
                "name": f"{mqtt_prefix_slug}_user_{name}",
                "unique_id": obj_id,
                "state_topic": state_topic,
                "value_template": "{{ 'ON' if (value_json.DACC | default('F') | upper) == 'F' else 'OFF' }}",
                "command_topic": f"{mqtt_prefix}/cmd/account/{aid}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "availability_topic": f"{mqtt_prefix}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
                "default_entity_id": f"switch.{obj_id}",
                "icon": "mdi:account",
            }
            payload = _apply_device(payload, "accounts")
            if _disc_publish("switch", obj_id, payload):
                published += 1

        # System temps -> sensor
        for e in entities:
            et = str(e.get("type") or "").lower()
            if et != "systems":
                continue
            _inc(et)
            try:
                sid = str(int(e.get("id")))
            except Exception:
                sid = "1"
            rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
            temps = rt.get("TEMP") or {}
            for key, label in (("IN", "Temp IN"), ("OUT", "Temp OUT")):
                obj_id = f"{mqtt_prefix_slug}_sys_{sid}_{key.lower()}"
                state_topic = f"{mqtt_prefix}/systems/{sid}"
                payload = {
                    "name": f"Sistema {sid} {label}",
                    "unique_id": obj_id,
                    "state_topic": state_topic,
                    "unit_of_measurement": "°C",
                    "value_template": f"{{{{ value_json.TEMP.{key} | default('') }}}}",
                    "device_class": "temperature",
                    "default_entity_id": f"sensor.{obj_id}",
                }
                payload = _apply_device(payload, "systems")
                if _disc_publish("sensor", obj_id, payload):
                    published += 1

            # System arm/mode description -> sensor (text)
            obj_id = f"{mqtt_prefix_slug}_sys_{sid}_arm_desc"
            state_topic = f"{mqtt_prefix}/systems/{sid}"
            payload = {
                "name": "Stato Scenari allarme",
                "unique_id": obj_id,
                "state_topic": state_topic,
                # Accept both raw payloads ({ARM:{D:...}}) and merged payloads ({realtime:{ARM:{D:...}}}).
                "value_template": "{{ (value_json.get('ARM', {}).get('D') or value_json.get('realtime', {}).get('ARM', {}).get('D') or '') }}",
                "icon": "mdi:shield",
                "default_entity_id": "sensor.stato_scenari_allarme",
            }
            payload = _apply_device(payload, "systems")
            if _disc_publish("sensor", obj_id, payload):
                published += 1
        logger.info(f"MQTT discovery: entities={len(entities)} per_type={per_type} published={published}")
        return published

    def _seed_partition_states(snapshot: dict):
        try:
            ents = snapshot.get("entities") or []
        except Exception:
            ents = []
        if not isinstance(ents, list):
            return
        for e in ents:
            if str(e.get("type") or "").lower() != "partitions":
                continue
            try:
                eid = str(int(e.get("id")))
            except Exception:
                continue
            rt = e.get("realtime") if isinstance(e.get("realtime"), dict) else {}
            arm = rt.get("ARM")
            if arm is None:
                continue
            publish("partitions", {"ID": eid, "ARM": arm})

    def _seed_output_states(snapshot: dict):
        try:
            ents = snapshot.get("entities") or []
        except Exception:
            ents = []
        if not isinstance(ents, list):
            return
        for e in ents:
            if str(e.get("type") or "").lower() != "outputs":
                continue
            try:
                eid = str(int(e.get("id")))
            except Exception:
                continue
            # Publish a minimal patch; publish() will merge static+realtime from state.
            publish("outputs", {"ID": eid})

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

    def _gsm_from_connection_item(item: dict) -> dict | None:
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

    def _parse_delay_seconds(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return max(0, int(value))
            except Exception:
                return None
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            try:
                return max(0, int(s))
            except Exception:
                return None
        parts = s.split(":")
        if len(parts) in (2, 3) and all(p.strip().isdigit() for p in parts):
            nums = [int(p.strip()) for p in parts]
            if len(nums) == 2:
                minutes, seconds = nums
                return max(0, minutes * 60 + seconds)
            hours, minutes, seconds = nums
            return max(0, hours * 3600 + minutes * 60 + seconds)
        return None

    def _augment_partition_delay_fields(item: dict) -> dict:
        if not isinstance(item, dict):
            return item
        arm_code = str(item.get("ARM") or "").upper()
        raw_time = item.get("TIME")
        if raw_time in (None, ""):
            raw_time = item.get("T")
        delay_sec = _parse_delay_seconds(raw_time)
        entry = 0
        exit = 0
        if delay_sec is not None:
            if arm_code == "IT":
                entry = delay_sec
            elif arm_code in ("OT", "DA"):
                exit = delay_sec
        return {**item, "ENTRY_DELAY": entry, "EXIT_DELAY": exit}

    class IconHttpNotifier:
        def __init__(self, enabled: bool, base_url: str, token: str, timeout_s: int):
            self._enabled = bool(enabled) and bool(str(base_url or "").strip())
            self._base_url = str(base_url or "").strip()
            self._token = str(token or "").strip()
            try:
                self._timeout_s = max(1, int(timeout_s))
            except Exception:
                self._timeout_s = 3
            self._last_state = None
            self._pending_task: asyncio.Task | None = None
            self._last_snapshot: dict | None = None
            self._seq = 0

        def enabled(self) -> bool:
            return self._enabled

        def _build_url(self, state: str) -> str:
            if not self._base_url:
                return ""
            try:
                parsed = urlparse(self._base_url)
                q = dict(parse_qsl(parsed.query, keep_blank_values=True))
                q["state"] = str(state)
                if self._token:
                    q["token"] = self._token
                return urlunparse(parsed._replace(query=urlencode(q)))
            except Exception:
                return ""

        def _safe_url_for_log(self, url: str) -> str:
            try:
                parsed = urlparse(url)
                q = dict(parse_qsl(parsed.query, keep_blank_values=True))
                if "token" in q and q["token"]:
                    q["token"] = "***"
                return urlunparse(parsed._replace(query=urlencode(q)))
            except Exception:
                return "<invalid url>"

        def _is_disarmed_code(self, code: str) -> bool:
            c = str(code or "").upper()
            return c in ("D", "DISARM", "DISINSERITO")

        def _is_partial_code(self, code: str) -> bool:
            c = str(code or "").upper()
            return c == "P" or c.startswith("P_")

        def _is_total_code(self, code: str) -> bool:
            c = str(code or "").upper()
            return c == "T" or c.startswith("T_")

        def _alarm_value_nonempty(self, value) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                s = value.strip()
                if s == "":
                    return False
                low = s.lower()
                if low in ("0", "false", "off", "no", "n", "ok"):
                    return False
                if s.isdigit():
                    try:
                        return int(s) != 0
                    except Exception:
                        return False
                return True
            if isinstance(value, (list, tuple, set)):
                return len(value) > 0
            if isinstance(value, dict):
                if not value:
                    return False
                for v in value.values():
                    if self._alarm_value_nonempty(v):
                        return True
                return False
            return True

        def _system_arm_code(self, sys_rt: dict, sys_st: dict) -> str:
            arm = None
            if isinstance(sys_rt, dict):
                arm = sys_rt.get("ARM")
            if arm is None and isinstance(sys_st, dict):
                arm = sys_st.get("ARM")
            if isinstance(arm, dict):
                return str(arm.get("S") or arm.get("s") or arm.get("CODE") or "").upper()
            return str(arm or "").upper()

        def _partition_arm_code(self, rt: dict) -> str:
            if not isinstance(rt, dict):
                return ""
            arm = rt.get("ARM")
            if isinstance(arm, dict):
                return str(arm.get("S") or arm.get("s") or arm.get("CODE") or "").upper()
            return str(arm or "").upper()

        def _compute_state(self, snapshot: dict) -> str | None:
            entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
            if not isinstance(entities, list):
                entities = []

            systems = [e for e in entities if str(e.get("type") or "").lower() == "systems"]
            partitions = [e for e in entities if str(e.get("type") or "").lower() == "partitions"]
            zones = [e for e in entities if str(e.get("type") or "").lower() == "zones"]

            sys_entity = None
            if systems:
                def _sys_key(e):
                    try:
                        return int(e.get("id"))
                    except Exception:
                        return 999999
                sys_entity = sorted(systems, key=_sys_key)[0]

            sys_rt = (sys_entity.get("realtime") if isinstance(sys_entity, dict) else None) or {}
            sys_st = (sys_entity.get("static") if isinstance(sys_entity, dict) else None) or {}
            if not isinstance(sys_rt, dict):
                sys_rt = {}
            if not isinstance(sys_st, dict):
                sys_st = {}

            code = self._system_arm_code(sys_rt, sys_st)
            is_disarmed = self._is_disarmed_code(code)

            # Follow the same state code shown by the "scudetti" in the Security UI circle:
            # T=totale, P=parziale, D=disinserito, A=allarme.
            c = str(code or "").upper()
            if c == "A" or c.startswith("A_") or c in ("AL", "ALARM"):
                return "alarm"
            if is_disarmed:
                return "disarm"
            if self._is_partial_code(code):
                return "arm_partial"
            return "arm_total"

        async def _http_get(self, url: str, state_name: str):
            def _do():
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=self._timeout_s) as res:
                    res.read(256)
                    return int(getattr(res, "status", 0) or 0)

            try:
                logger.info("Icon HTTP GET stato=%s url=%s", state_name, self._safe_url_for_log(url))
                status = await asyncio.to_thread(_do)
                if status and status >= 400:
                    logger.warning("Icon HTTP GET stato=%s fallita (HTTP %s)", state_name, status)
            except Exception as exc:
                logger.warning("Icon HTTP GET stato=%s fallita (%s)", state_name, type(exc).__name__)

        async def _debounced_eval_and_notify(self, seq: int):
            try:
                await asyncio.sleep(0.35)
            except asyncio.CancelledError:
                return
            if seq != self._seq:
                return
            snapshot = self._last_snapshot
            if not isinstance(snapshot, dict):
                return
            state_name = self._compute_state(snapshot)
            if not state_name:
                return
            if seq != self._seq:
                return
            if state_name == self._last_state:
                return
            url = self._build_url(state_name)
            if not url:
                return
            self._last_state = state_name
            asyncio.create_task(self._http_get(url, state_name))

        def maybe_notify(self, snapshot: dict):
            if not self._enabled:
                return
            if isinstance(snapshot, dict):
                self._last_snapshot = snapshot
            self._seq += 1
            try:
                if self._pending_task is not None and not self._pending_task.done():
                    self._pending_task.cancel()
            except Exception:
                pass
            self._pending_task = asyncio.create_task(self._debounced_eval_and_notify(self._seq))

    icon_notifier = IconHttpNotifier(
        enabled=icon_http_enabled,
        base_url=icon_http_base_url,
        token=icon_http_token,
        timeout_s=icon_http_timeout,
    )

    def _brief_outputs_for_log(updates, limit: int = 12):
        if not isinstance(updates, list):
            return None
        out = []
        for it in updates:
            if not isinstance(it, dict):
                continue
            row = {"ID": it.get("ID")}
            for k in ("STA", "LEV", "DIM", "POS", "TYP", "CAT"):
                if k in it:
                    row[k] = it.get(k)
            out.append(row)
            if len(out) >= int(limit):
                break
        return out

    async def on_status_updates(entity_type: str, updates):
        try:
            if output_debug_verbose and entity_type in ("lights", "switches", "covers", "outputs"):
                try:
                    brief = _brief_outputs_for_log(updates)
                    if brief:
                        logger.info(
                            "WS1 outputs update (%s) n=%s sample=%s",
                            entity_type,
                            len(updates) if isinstance(updates, list) else 0,
                            brief,
                        )
                except Exception:
                    pass
            if entity_type == "partitions" and isinstance(updates, list):
                updates = [
                    _augment_partition_delay_fields(item) if isinstance(item, dict) else item
                    for item in updates
                ]
                try:
                    brief = []
                    for it in updates:
                        if not isinstance(it, dict):
                            continue
                        brief.append(
                            {
                                "ID": it.get("ID"),
                                "ARM": it.get("ARM"),
                                "T": it.get("T"),
                            }
                        )
                    if brief:
                        logger.info(f"WS1 partitions update: {brief}")
                except Exception:
                    pass
            if entity_type in ("schedulers",):
                state.apply_static_update(entity_type, updates)
            else:
                state.apply_realtime_update(entity_type, updates)
        except Exception as exc:
            logger.error(f"Debug state update error: {exc}")
        if isinstance(updates, list):
            for item in updates:
                if isinstance(item, dict):
                    publish(entity_type, item)
                    if entity_type in ("lights", "switches", "covers"):
                        try:
                            publish("outputs", item)
                        except Exception:
                            pass
                    if entity_type == "schedulers":
                        # Publish discovery immediately when a scheduler appears/updates,
                        # so the HA switch shows up without requiring manual republish_discovery.
                        try:
                            sid = str(int(item.get("ID")))
                            obj_id = f"{mqtt_prefix_slug}_sched_{sid}"
                            state_topic = f"{mqtt_prefix}/schedulers/{sid}"
                            name = str(item.get("DES") or f"Timer {sid}").strip() or f"Timer {sid}"
                            payload = {
                                "name": name,
                                "unique_id": obj_id,
                                "state_topic": state_topic,
                                "value_template": "{{ 'ON' if (value_json.EN | default('F') | upper) in ['T','1','ON','TRUE','YES'] else 'OFF' }}",
                                "command_topic": f"{mqtt_prefix}/cmd/scheduler/{sid}",
                                "payload_on": "ON",
                                "payload_off": "OFF",
                                "state_on": "ON",
                                "state_off": "OFF",
                                "availability_topic": f"{mqtt_prefix}/status",
                                "payload_available": "online",
                                "payload_not_available": "offline",
                                "default_entity_id": f"switch.{obj_id}",
                                "icon": "mdi:clock-outline",
                            }
                            try:
                                payload["device"] = _disc_device(state.snapshot(), "schedulers", "Programmatori")
                            except Exception:
                                pass
                            _disc_publish("switch", obj_id, payload)
                        except Exception:
                            pass
                    if entity_type == "connection":
                        gsm = _gsm_from_connection_item(item)
                        if gsm:
                            publish("gsm", gsm)
        elif isinstance(updates, dict):
            publish(entity_type, updates)

        if icon_notifier.enabled() and entity_type in ("systems", "partitions", "zones"):
            try:
                icon_notifier.maybe_notify(state.snapshot())
            except Exception as exc:
                logger.error("Icon HTTP notify error: %s", exc)

        # Derived sensors: list of alarm zones per partition.
        try:
            if entity_type in ("zones", "partitions", "systems"):
                updates_list = updates if isinstance(updates, list) else ([updates] if isinstance(updates, dict) else [])
                if entity_type == "partitions" and updates_list:
                    pids = []
                    for it in updates_list:
                        if not isinstance(it, dict):
                            continue
                        try:
                            pids.append(int(str(it.get("ID")).strip()))
                        except Exception:
                            continue
                    for pid in sorted(set([p for p in pids if p > 0])):
                        # Clear derived alarms when a partition is disarmed.
                        try:
                            merged = state.get_merged("partitions", str(pid))
                        except Exception:
                            merged = None
                        if isinstance(merged, dict) and _partition_is_disarmed(merged):
                            _alarm_zone_last.pop(int(pid), None)
                        publish_alarm_zones_for_partition(pid)
                elif entity_type == "zones" and updates_list:
                    touched = set()
                    for it in updates_list:
                        if not isinstance(it, dict):
                            continue
                        zid = it.get("ID")
                        if zid is None:
                            continue
                        try:
                            zid_s = str(int(str(zid).strip()))
                        except Exception:
                            zid_s = str(zid).strip()
                        if not zid_s:
                            continue
                        try:
                            merged = state.get_merged("zones", zid_s)
                        except Exception:
                            merged = None
                        if not isinstance(merged, dict):
                            continue
                        st = merged.get("static") if isinstance(merged.get("static"), dict) else {}
                        part_ids = _partition_ids_from_state()
                        pids = _decode_zone_prt_partition_ids(st.get("PRT"), part_ids)
                        for pid in pids:
                            touched.add(pid)
                    if touched:
                        for pid in sorted(touched):
                            publish_alarm_zones_for_partition(pid)
                    else:
                        publish_alarm_zones_for_all_partitions()
                else:
                    publish_alarm_zones_for_all_partitions()
        except Exception:
            pass

        # LOGS-derived alarm zones: add on ZALARM events.
        try:
            if entity_type == "logs":
                updates_list = updates if isinstance(updates, list) else ([updates] if isinstance(updates, dict) else [])
                if updates_list:
                    for it in updates_list:
                        if not isinstance(it, dict):
                            continue
                        typ = str(it.get("TYPE") or "").strip().upper()
                        ev = str(it.get("EV") or "").strip().casefold()
                        if (typ != "ZALARM") and ("allarme zona" not in ev):
                            continue
                        zname = str(it.get("I1") or "").strip()
                        zid, disp = _find_zone_by_name(zname)
                        # Determine partition(s) for this zone.
                        pids = []
                        if zid:
                            try:
                                merged = state.get_merged("zones", str(zid))
                            except Exception:
                                merged = None
                            if isinstance(merged, dict):
                                st = merged.get("static") if isinstance(merged.get("static"), dict) else {}
                                pids = _decode_zone_prt_partition_ids(st.get("PRT"), _partition_ids_from_state())
                        if not pids:
                            armed = _armed_partition_ids()
                            if len(armed) == 1:
                                pids = armed
                            elif armed:
                                # fallback: associate to all armed partitions to avoid losing info
                                pids = armed
                            else:
                                pids = [1]
                        _set_alarm_zone_for_partitions(disp or zname, pids)
                    publish_alarm_zones_for_all_partitions()
        except Exception:
            pass

    manager = WebSocketManager(
        ip=ksenia_host,
        pin=ksenia_pin,
        port=ksenia_port,
        logger=logger,
        debug_thermostats=debug_thermostats,
        output_debug_verbose=output_debug_verbose,
        reconnect_cooldown_sec=ws_reconnect_cooldown_sec,
    )
    try:
        manager_ref["manager"] = manager
    except Exception:
        pass

    # Track WS1 status in UI meta.
    try:
        state.set_ws1_status(False)
    except Exception:
        pass

    async def _after_reconnect(read_data, realtime_initial, system_version):
        logger.info("WebSocket riallineata: ricarico dati (static+realtime)")
        try:
            state.set_ws1_status(True)
        except Exception:
            pass
        try:
            state.set_initial_data(read_data, realtime_initial)
        except Exception as exc:
            logger.error(f"Debug initial ingest error (reconnect): {exc}")
        try:
            if icon_notifier.enabled():
                icon_notifier.maybe_notify(state.snapshot())
        except Exception as exc:
            logger.error(f"Icon HTTP notify error (reconnect): {exc}")

        # Publish derived partition sensors once at reconnect.
        try:
            publish_alarm_zones_for_all_partitions()
        except Exception:
            pass

        # Seed outputs state topics so HA entities are not stuck on "unknown" until the first realtime update.
        try:
            _seed_output_states(state.snapshot())
        except Exception:
            pass

        # Seed event logs into Debug UI state (they aren't part of READ/REALTIME payloads).
        try:
            logs_state = getattr(manager, "_logs_state", None) or {}
            logs_list = logs_state.get("LOGS") if isinstance(logs_state, dict) else None
            if isinstance(logs_list, list) and logs_list:
                state.apply_realtime_update("logs", logs_list)
        except Exception as exc:
            logger.error(f"Errore riallineando logs (reconnect): {exc}")

        # Seed schedulers from static read data.
        try:
            scheds = await manager.getSchedulers()
            if isinstance(scheds, list) and scheds:
                state.apply_static_update("schedulers", scheds)
        except Exception as exc:
            logger.error(f"Errore riallineando schedulers (reconnect): {exc}")

        # Seed accounts (users) from static read data so MQTT state topics are populated.
        try:
            accts = (read_data or {}).get("CFG_ACCOUNTS") if isinstance(read_data, dict) else None
            if isinstance(accts, list) and accts:
                state.apply_static_update("accounts", accts)
                for item in accts:
                    if isinstance(item, dict):
                        publish("accounts", item)
        except Exception as exc:
            logger.error(f"Errore riallineando accounts (reconnect): {exc}")

        # Ensure partitions include computed entry/exit delay fields immediately.
        try:
            payload = (realtime_initial or {}).get("PAYLOAD", {}) if isinstance(realtime_initial, dict) else {}
            parts = payload.get("STATUS_PARTITIONS")
            if isinstance(parts, list):
                await on_status_updates("partitions", parts)
        except Exception as exc:
            logger.error(f"Errore riallineando partizioni (reconnect): {exc}")

        # Enrich systems with panel model/version info (SYSTEM_VERSION).
        try:
            sv = system_version or {}
            if isinstance(sv, dict) and sv:
                sys_id = int((sv.get("ID") or 1))
                enrich = {"ID": sys_id}
                for k in ("MODEL", "BRAND", "BOOT", "IP", "FS", "SSL", "OS", "VER_LITE", "PRG_CHECK"):
                    if k in sv:
                        enrich[k] = sv.get(k)
                state.apply_static_update("systems", [enrich])
        except Exception as exc:
            logger.error(f"Errore leggendo SYSTEM_VERSION (reconnect): {exc}")

        # Republish full state to MQTT so HA sees configuration changes without add-on restart.
        try:
            for item in await manager.getLights():
                publish("lights", item)
                publish("outputs", item)
            for item in await manager.getRolls():
                publish("covers", item)
                publish("outputs", item)
            for item in await manager.getSwitches():
                publish("switches", item)
                publish("outputs", item)
            scens = await manager.getScenarios()
            if isinstance(scens, list):
                try:
                    state.apply_static_update("scenarios", scens)
                except Exception:
                    pass
                for item in scens:
                    if isinstance(item, dict):
                        publish("scenarios", item)
            for item in await manager.getDom():
                publish("domus", item)
            for item in await manager.getThermostats():
                publish("thermostats", item)
            for item in await manager.getSystem():
                publish("systems", item)
            logs_state = getattr(manager, "_logs_state", None) or {}
            logs_list = logs_state.get("LOGS") if isinstance(logs_state, dict) else None
            if isinstance(logs_list, list):
                for item in logs_list:
                    if isinstance(item, dict):
                        publish("logs", item)
            conn_list = (
                (realtime_initial or {}).get("PAYLOAD", {}).get("STATUS_CONNECTION", []) or []
                if isinstance(realtime_initial, dict)
                else []
            )
            for item in conn_list:
                if isinstance(item, dict):
                    publish("connection", item)
                    gsm = _gsm_from_connection_item(item)
                    if gsm:
                        publish("gsm", gsm)
        except Exception as exc:
            logger.error(f"Errore pubblicando stato (reconnect): {exc}")

    manager.set_on_reconnect(_after_reconnect)

    manager.register_listener("lights", lambda updates: on_status_updates("lights", updates))
    manager.register_listener("covers", lambda updates: on_status_updates("covers", updates))
    manager.register_listener("switches", lambda updates: on_status_updates("switches", updates))
    manager.register_listener("domus", lambda updates: on_status_updates("domus", updates))
    manager.register_listener("powerlines", lambda updates: on_status_updates("powerlines", updates))
    manager.register_listener("partitions", lambda updates: on_status_updates("partitions", updates))
    manager.register_listener("zones", lambda updates: on_status_updates("zones", updates))
    manager.register_listener("systems", lambda updates: on_status_updates("systems", updates))
    manager.register_listener("connection", lambda updates: on_status_updates("connection", updates))
    manager.register_listener("thermostats", lambda updates: on_status_updates("thermostats", updates))
    async def _on_thermostats_cfg(updates):
        try:
            state.apply_static_update("thermostats", updates)
        except Exception as exc:
            logger.error(f"Debug thermostats cfg ingest error: {exc}")

    manager.register_listener("thermostats_cfg", _on_thermostats_cfg)
    manager.register_listener("logs", lambda updates: on_status_updates("logs", updates))
    manager.register_listener("schedulers", lambda updates: on_status_updates("schedulers", updates))

    async def run():
        loop = asyncio.get_running_loop()
        try:
            manager_ref["loop"] = loop
        except Exception:
            pass

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        ssl_context.verify_mode = ssl.CERT_NONE
        ssl_context.options |= 0x4

        class WebCommandSession:
            def __init__(self, ws, login_id: int, pin: str, expires_at: float, secure: bool, opener=None):
                self.ws = ws
                self.login_id = int(login_id)
                self.pin = str(pin)
                self.expires_at = float(expires_at)
                self.secure = bool(secure)
                self._opener = opener
                self._lock = asyncio.Lock()
                self._cmd_seq = 1000

            async def close(self):
                try:
                    await self.ws.close()
                except Exception:
                    pass

            async def _send_cmd_usr(self, payload_type: str, payload: dict) -> dict | None:
                self._cmd_seq += 1
                cmd_id = str(self._cmd_seq)
                msg = {
                    "SENDER": "HomeAssistant",
                    "RECEIVER": "",
                    "CMD": "CMD_USR",
                    "ID": cmd_id,
                    "PAYLOAD_TYPE": str(payload_type),
                    "PAYLOAD": payload,
                    "TIMESTAMP": str(int(time.time())),
                    "CRC_16": "0x0000",
                }
                raw = addCRC(json.dumps(msg, separators=(",", ":"), ensure_ascii=False))
                await self.ws.send(raw)

                # Wait for the matching CMD_USR_RES.
                deadline = time.time() + 15
                while True:
                    timeout = max(0.2, deadline - time.time())
                    if timeout <= 0:
                        raise TimeoutError("CMD_USR_RES timeout")
                    txt = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
                    try:
                        resp = json.loads(txt)
                    except Exception:
                        continue
                    if resp.get("CMD") != "CMD_USR_RES":
                        continue
                    if str(resp.get("ID")) != cmd_id:
                        continue
                    return resp.get("PAYLOAD") or {}

            async def execute_scenario(self, scenario_id: int) -> bool:
                async with self._lock:
                    payload = {
                        "ID_LOGIN": str(self.login_id),
                        "PIN": self.pin,
                        "SCENARIO": {"ID": str(int(scenario_id))},
                    }
                    resp = await self._send_cmd_usr("CMD_EXE_SCENARIO", payload)
                    if isinstance(resp, dict) and resp.get("RESULT") and resp.get("RESULT") != "OK":
                        return False
                    return True

            async def set_account_enabled(self, account_patch: dict) -> bool:
                if not isinstance(account_patch, dict):
                    return False
                acct = dict(account_patch)
                if acct.get("ID") is None:
                    return False
                async with self._lock:
                    patch = {"CFG_ACCOUNTS": [acct]}
                    try:
                        resp = await writeCfgTyped(
                            self.ws,
                            self.login_id,
                            logger,
                            "CFG_ALL",
                            patch,
                            pin=self.pin,
                        )
                        return isinstance(resp, dict) and resp.get("RESULT") == "OK"
                    except Exception as exc:
                        logger.error("set_account_enabled failed: %s", exc)
                        return False

            async def set_partition(self, partition_id: int, mod: str) -> bool:
                async with self._lock:
                    mod_raw = str(mod or "").strip().upper()
                    if mod_raw in ("ARM", "DELAY", "ARM_DELAY"):
                        mod_raw = "A"
                    elif mod_raw in ("INSTANT", "ARM_INSTANT", "ARM_NOW"):
                        mod_raw = "I"
                    elif mod_raw in ("DISARM",):
                        mod_raw = "D"
                    payload = {
                        "ID_LOGIN": str(self.login_id),
                        "PIN": self.pin,
                        "PARTITION": {"ID": str(int(partition_id)), "MOD": str(mod_raw)},
                    }
                    resp = await self._send_cmd_usr("CMD_ARM_PARTITION", payload)
                    if isinstance(resp, dict) and resp.get("RESULT") and resp.get("RESULT") != "OK":
                        return False
                    return True

            async def set_zone_bypass(self, zone_id: int, byp: str) -> bool:
                async with self._lock:
                    byp_raw = str(byp or "").strip().upper()
                    byp_map = {"ON": "AUTO", "OFF": "NO", "1": "AUTO", "0": "NO"}
                    byp_norm = byp_map.get(byp_raw, byp_raw)
                    if byp_norm not in ("AUTO", "NO", "TGL", "ON", "OFF"):
                        raise ValueError("invalid BYP (use ON/OFF/TGL)")
                    payload = {
                        "ID_LOGIN": str(self.login_id),
                        "PIN": self.pin,
                        "ZONE": {"ID": str(int(zone_id)), "BYP": str(byp_norm)},
                    }
                    resp = await self._send_cmd_usr("CMD_BYP_ZONE", payload)
                    if isinstance(resp, dict) and resp.get("RESULT") and resp.get("RESULT") != "OK":
                        return False
                    return True

        import threading

        ui_tags_lock = threading.Lock()
        ui_tags_path = Path("/data/ui_tags.json")

        def _coerce_bool(value, default=True):
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("1", "true", "t", "yes", "y", "on"):
                return True
            if s in ("0", "false", "f", "no", "n", "off", ""):
                return False
            return bool(default)

        def _load_ui_tags_file():
            if not ui_tags_path.exists():
                return {
                    "outputs": {},
                    "scenarios": {},
                    "tag_styles": {
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
                    },
                }
            try:
                raw = json.loads(ui_tags_path.read_text(encoding="utf-8")) or {}
            except Exception:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            for key in ("outputs", "scenarios", "tag_styles"):
                if not isinstance(raw.get(key), dict):
                    raw[key] = {}
            # Seed defaults if tag_styles is still empty (user can overwrite from UI).
            if not raw.get("tag_styles"):
                raw["tag_styles"] = {
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
            return raw

        def _save_ui_tags_file(data):
            try:
                ui_tags_path.parent.mkdir(parents=True, exist_ok=True)
                ui_tags_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.error(f"Impossibile salvare ui_tags: {exc}")

        async def _open_command_ws(pin: str) -> tuple[object, int, bool]:
            """
            Open the dedicated command websocket (WS2) with bounded timeouts.
            If the plain ws:// fails or times out, fall back to wss://.
            """

            async def _try_connect(uri: str, use_ssl: bool):
                # Keep connection + login bounded to avoid freezing the UI request.
                conn_timeout = 8
                login_timeout = 8
                try:
                    ws = await asyncio.wait_for(
                        websockets.connect(
                            uri,
                            ssl=ssl_context if use_ssl else None,
                            subprotocols=["KS_WSOCK"],
                        ),
                        timeout=conn_timeout,
                    )
                except Exception as exc:
                    logger.warning(f"WS2 connect failed ({uri}): {exc}")
                    raise
                try:
                    login_id = await asyncio.wait_for(ws_login(ws, str(pin), logger), timeout=login_timeout)
                except Exception as exc:
                    # If login phase fails, close and re-raise so caller can fall back.
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    logger.warning(f"WS2 login failed ({uri}): {exc}")
                    raise
                if not login_id or int(login_id) <= 0:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    logger.warning(f"WS2 login_id invalid ({uri})")
                    raise PermissionError("login_failed")
                logger.info(f"WS2 connected via {'wss' if use_ssl else 'ws'} (login_id={login_id})")
                return ws, int(login_id)

            uri_ws = f"ws://{ksenia_host}:{ksenia_port}/KseniaWsock"
            try:
                ws, login_id = await _try_connect(uri_ws, use_ssl=False)
                return ws, login_id, False
            except Exception:
                pass

            uri_wss = f"wss://{ksenia_host}:{ksenia_port}/KseniaWsock"
            ws, login_id = await _try_connect(uri_wss, use_ssl=True)
            return ws, login_id, True

        class WebCommandHub:
            """
            Singleton "command WS" hub:
            - only 1 websocket to the panel for PIN-based (CMD_USR) actions
            - multiple UI tokens can share the same session
            - session/start always replaces the active session (as requested)
            - websocket auto-closes after idle_timeout_sec, but the token stays valid until expires_at
            """

            def __init__(self, idle_timeout_sec: int):
                try:
                    idle = int(idle_timeout_sec)
                except Exception:
                    idle = 20
                self._idle_timeout_sec = max(0, min(3600, idle))
                self._lock = asyncio.Lock()
                self._tokens: dict[str, float] = {}
                self._sess: WebCommandSession | None = None
                self._pin: str | None = None
                self._expires_at: float = 0.0
                self._last_used: float = 0.0
                self._watch_task: asyncio.Task | None = None

            def _ensure_watch_task(self):
                if self._watch_task is None or self._watch_task.done():
                    self._watch_task = asyncio.create_task(self._watchdog())

            def _cleanup_tokens_nolock(self, now: float):
                expired = [t for t, exp in self._tokens.items() if float(exp) <= now]
                for t in expired:
                    self._tokens.pop(t, None)

            async def _close_ws_nolock(self):
                if self._sess is None:
                    return
                try:
                    await self._sess.close()
                except Exception:
                    pass
                self._sess = None

            async def _reset_session_nolock(self):
                await self._close_ws_nolock()
                self._tokens = {}
                self._pin = None
                self._expires_at = 0.0
                self._last_used = 0.0

            async def start(self, pin: str, minutes: int | None = None) -> tuple[str, float]:
                pin = str(pin or "").strip()
                if not pin:
                    raise ValueError("pin_required")
                try:
                    logger.info(f"WS2 start: PIN length={len(pin)}")
                except Exception:
                    pass
                now = time.time()
                mins = minutes if minutes is not None else web_pin_session_minutes_default
                try:
                    mins = int(mins)
                except Exception:
                    mins = web_pin_session_minutes_default
                mins = max(1, min(240, mins))
                expires_at = now + (mins * 60)

                async with self._lock:
                    # Replace any existing session/tokens (requested behavior).
                    await self._reset_session_nolock()
                    ws, login_id, secure = await _open_command_ws(pin)
                    self._pin = str(pin)
                    self._expires_at = float(expires_at)
                    self._last_used = now
                    self._sess = WebCommandSession(
                        ws=ws,
                        login_id=login_id,
                        pin=self._pin,
                        expires_at=self._expires_at,
                        secure=secure,
                        opener=_open_command_ws,
                    )
                    token = secrets.token_urlsafe(24)
                    self._tokens[token] = self._expires_at
                    self._ensure_watch_task()
                    return token, self._expires_at

            async def end(self, token: str | None) -> bool:
                token = str(token or "").strip()
                if not token:
                    return False
                now = time.time()
                async with self._lock:
                    self._cleanup_tokens_nolock(now)
                    existed = token in self._tokens
                    self._tokens.pop(token, None)
                    if not self._tokens:
                        await self._reset_session_nolock()
                    return bool(existed)

            async def status(self, token: str | None) -> float | None:
                token = str(token or "").strip()
                if not token:
                    return None
                now = time.time()
                async with self._lock:
                    self._cleanup_tokens_nolock(now)
                    exp = self._tokens.get(token)
                    if exp is None:
                        return None
                    if now >= float(exp):
                        self._tokens.pop(token, None)
                        if not self._tokens:
                            await self._reset_session_nolock()
                        return None
                    return float(exp)

            async def get_session(self, token: str | None) -> WebCommandSession | None:
                token = str(token or "").strip()
                if not token:
                    return None
                now = time.time()
                async with self._lock:
                    self._cleanup_tokens_nolock(now)
                    exp = self._tokens.get(token)
                    if exp is None:
                        return None
                    if now >= float(exp):
                        self._tokens.pop(token, None)
                        if not self._tokens:
                            await self._reset_session_nolock()
                        return None
                    # Reopen WS on-demand if it was closed by idle watchdog.
                    if self._sess is None:
                        if not self._pin:
                            return None
                        ws, login_id, secure = await _open_command_ws(self._pin)
                        self._sess = WebCommandSession(
                            ws=ws,
                            login_id=login_id,
                            pin=self._pin,
                            expires_at=float(exp),
                            secure=secure,
                            opener=_open_command_ws,
                        )
                    self._expires_at = float(exp)
                    self._last_used = now
                    self._ensure_watch_task()
                    return self._sess

            async def _watchdog(self):
                while True:
                    await asyncio.sleep(1)
                    now = time.time()
                    async with self._lock:
                        self._cleanup_tokens_nolock(now)
                        if not self._tokens:
                            await self._reset_session_nolock()
                            return
                        # Auto-close WS after idle, keeping the token alive.
                        if (
                            self._sess is not None
                            and self._idle_timeout_sec > 0
                            and self._last_used > 0
                            and (now - self._last_used) >= self._idle_timeout_sec
                        ):
                            await self._close_ws_nolock()

        web_hub = WebCommandHub(security_cmd_ws_idle_timeout_sec)

        async def _ws1_status_poller():
            last = None
            while True:
                try:
                    connected = bool(getattr(manager, "_running", False) and getattr(manager, "_ws", None) is not None)
                    if last is None or connected != last:
                        try:
                            state.set_ws1_status(connected)
                        except Exception:
                            pass
                        try:
                            logger.info(f"WS1 status: {'connected' if connected else 'disconnected'}")
                        except Exception:
                            pass
                        last = connected
                except Exception:
                    pass
                await asyncio.sleep(1.0)

        def _execute_command(payload: dict):
            entity_type = str(payload.get("type") or "")
            action = str(payload.get("action") or "")
            value = payload.get("value", None)
            token = payload.get("token")

            if entity_type in ("ws", "websocket"):
                if action in ("reconnect", "reconnect_main", "ws1_reconnect"):
                    fut = asyncio.run_coroutine_threadsafe(manager.force_reconnect(), loop)
                    try:
                        fut.result(timeout=1)
                    except Exception:
                        pass
                    try:
                        state.set_ws1_status(False)
                    except Exception:
                        pass
                    return {"ok": True}
                return {"ok": False, "error": "unsupported ws action"}

            # PIN session management (works even if WS is offline).
            if entity_type == "session":
                if action == "start":
                    if isinstance(value, dict):
                        pin = str(value.get("pin") or "").strip()
                        minutes = value.get("minutes", None)
                    else:
                        pin = str(value or "").strip()
                        minutes = None
                    if not pin:
                        return {"ok": False, "error": "pin_required"}
                    logger.info(f"WS2 session start requested (minutes={minutes or web_pin_session_minutes_default})")
                    fut = asyncio.run_coroutine_threadsafe(web_hub.start(pin, minutes=minutes), loop)
                    try:
                        tok, exp = fut.result(timeout=20)
                    except concurrent.futures.TimeoutError:
                        try:
                            fut.cancel()
                        except Exception:
                            pass
                        logger.warning("WS2 session start timeout")
                        return {"ok": False, "error": "timeout"}
                    except Exception as exc:
                        logger.error(f"WS2 session start error: {exc}")
                        return {"ok": False, "error": str(exc) or "session_start_failed"}
                    return {"ok": True, "token": tok, "expires_at": exp}
                if action == "end":
                    fut = asyncio.run_coroutine_threadsafe(web_hub.end(token), loop)
                    try:
                        ok = bool(fut.result(timeout=10))
                    except Exception as exc:
                        return {"ok": False, "error": str(exc)}
                    if ok:
                        return {"ok": True}
                    return {"ok": False, "error": "invalid_token"}
                if action == "status":
                    fut = asyncio.run_coroutine_threadsafe(web_hub.status(token), loop)
                    try:
                        exp = fut.result(timeout=10)
                    except Exception as exc:
                        return {"ok": False, "error": str(exc)}
                    if exp is None:
                        return {"ok": False, "error": "invalid_token"}
                    return {"ok": True, "expires_at": exp}
                return {"ok": False, "error": "unsupported session action"}

            if entity_type == "ui_tags":
                if action not in ("set", "update"):
                    return {"ok": False, "error": "unsupported ui_tags action"}
                if not isinstance(value, dict):
                    return {"ok": False, "error": "value must be an object"}
                entity_id = payload.get("id")
                try:
                    entity_id_int = int(entity_id)
                except Exception:
                    return {"ok": False, "error": "invalid id"}
                target_type = str(value.get("target_type") or "").strip().lower()
                if target_type not in ("outputs", "scenarios"):
                    return {"ok": False, "error": "invalid target_type"}
                tag = str(value.get("tag") or "").strip()
                visible = _coerce_bool(value.get("visible", True), True)
                with ui_tags_lock:
                    data = _load_ui_tags_file()
                    target_map = data.get(target_type)
                    if not isinstance(target_map, dict):
                        target_map = {}
                        data[target_type] = target_map
                    key = str(entity_id_int)
                    entry = {}
                    if tag:
                        entry["tag"] = tag
                    if not visible:
                        entry["visible"] = False
                    if entry:
                        target_map[key] = entry
                    else:
                        target_map.pop(key, None)
                    _save_ui_tags_file(data)
                return {"ok": True, "id": entity_id_int, "type": target_type, "entry": entry}

            if entity_type == "tag_styles":
                if action not in ("set", "update", "delete"):
                    return {"ok": False, "error": "unsupported tag_styles action"}
                if action == "delete":
                    tag_name = ""
                    if isinstance(value, dict):
                        tag_name = str(value.get("tag") or "").strip()
                    else:
                        tag_name = str(value or "").strip()
                    if not tag_name:
                        return {"ok": False, "error": "tag_required"}
                    with ui_tags_lock:
                        data = _load_ui_tags_file()
                        styles = data.get("tag_styles")
                        if not isinstance(styles, dict):
                            styles = {}
                            data["tag_styles"] = styles
                        existed = tag_name in styles
                        styles.pop(tag_name, None)
                        _save_ui_tags_file(data)
                    return {"ok": True, "deleted": bool(existed), "tag": tag_name}

                if not isinstance(value, dict):
                    return {"ok": False, "error": "value must be an object"}
                tag_name = str(value.get("tag") or "").strip()
                if not tag_name:
                    return {"ok": False, "error": "tag_required"}
                style = {}
                for k in ("icon_on", "icon_off", "color_on", "color_off", "svg_on", "svg_off"):
                    v = value.get(k)
                    if v is None:
                        continue
                    s = str(v).strip()
                    if s:
                        style[k] = s
                with ui_tags_lock:
                    data = _load_ui_tags_file()
                    styles = data.get("tag_styles")
                    if not isinstance(styles, dict):
                        styles = {}
                        data["tag_styles"] = styles
                    if action in ("set", "update"):
                        if style:
                            styles[tag_name] = {**(styles.get(tag_name) or {}), **style}
                        else:
                            styles.pop(tag_name, None)
                    _save_ui_tags_file(data)
                    return {"ok": True, "tag": tag_name, "style": styles.get(tag_name) or {}}

            if entity_type == "mqtt" and action == "cleanup_discovery":
                try:
                    snap = state.snapshot()
                    ents = snap.get("entities") or []
                    prefixes = {str(mqtt_prefix).strip(), str(mqtt_prefix_slug).strip()}

                    def _obj_ids(pf: str):
                        out = []
                        for e in ents:
                            et = str(e.get("type") or "").lower()
                            try:
                                eid = str(int(e.get("id")))
                            except Exception:
                                continue
                            if et == "zones":
                                out.append(("binary_sensor", f"{pf}_zone_{eid}"))
                                out.append(("binary_sensor", f"{pf}_zone_{eid}_alarm"))
                                out.append(("binary_sensor", f"{pf}_zone_{eid}_bypass"))
                                out.append(("binary_sensor", f"{pf}_zone_{eid}_tamper"))
                                out.append(("binary_sensor", f"{pf}_zone_{eid}_mask"))
                                out.append(("switch", f"{pf}_zone_{eid}_bypass_ctrl"))
                            elif et == "partitions":
                                out.append(("binary_sensor", f"{pf}_part_{eid}"))
                                # Rimuovi eventuali vecchi config alarm_control_panel residui
                                out.append(("alarm_control_panel", f"{pf}_part_{eid}"))
                            elif et == "outputs":
                                out.append(("switch", f"{pf}_out_{eid}"))
                                # Rimuovi anche eventuali vecchi binary_sensor_<out> lasciati da versioni precedenti.
                                out.append(("binary_sensor", f"{pf}_out_{eid}"))
                            elif et == "scenarios":
                                out.append(("script", f"{pf}_scen_{eid}"))
                                out.append(("button", f"{pf}_scen_{eid}"))
                            elif et == "thermostats":
                                out.append(("climate", f"{pf}_therm_{eid}"))
                            elif et == "accounts":
                                out.append(("binary_sensor", f"{pf}_acc_{eid}"))
                                out.append(("switch", f"{pf}_acc_{eid}"))
                                out.append(("switch", f"{pf}_user_{eid}"))
                            elif et == "systems":
                                for key in ("in", "out"):
                                    out.append(("sensor", f"{pf}_sys_{eid}_{key}"))
                            elif et == "schedulers":
                                out.append(("switch", f"{pf}_sched_{eid}"))
                        # Panel buttons (no entity list)
                        out.append(("button", f"{pf}_panel_clear_memories"))
                        out.append(("button", f"{pf}_panel_clear_communications"))
                        out.append(("button", f"{pf}_panel_clear_faults"))
                        return out

                    topics = []
                    for pf in prefixes:
                        if pf:
                            topics.extend(_obj_ids(pf))
                    cleared = 0
                    for domain, obj_id in topics:
                        topic = f"homeassistant/{domain}/{obj_id}/config"
                        try:
                            mqttc.publish(topic, "", retain=True)
                            cleared += 1
                        except Exception:
                            pass
                    logger.info(f"MQTT cleanup_discovery cleared {cleared} configs")
                    return {"ok": True, "cleared": cleared}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

            if entity_type == "mqtt" and action == "republish_discovery":
                try:
                    snap = state.snapshot()
                    ents = snap.get("entities") or []
                    logger.info(f"MQTT republish_discovery: snapshot entities={len(ents)}")
                    published = publish_discovery(snap)
                    try:
                        _seed_partition_states(snap)
                    except Exception:
                        pass
                    logger.info(f"MQTT republish_discovery published {published} configs")
                    return {"ok": True, "published": published, "entities": len(ents)}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

            entity_id = payload.get("id")

            try:
                entity_id_int = int(entity_id)
            except Exception:
                return {"ok": False, "error": "invalid id"}

            async def _coro():
                sess = await web_hub.get_session(token)
                # If configured, security commands must go through the dedicated "PIN session" WS (ws2)
                # so the UI can use any panel PIN without storing it in the add-on config.
                requires_session = bool(web_pin_session_required)
                if entity_type in ("scenarios", "partitions", "zones", "accounts") and requires_session and not sess:
                    return {"ok": False, "error": "pin_session_required"}

                # Allow security commands via dedicated web session even if the main WS is offline.
                if not getattr(manager, "_running", False) and not (
                    sess and entity_type in ("scenarios", "partitions", "zones", "accounts")
                ):
                    return {"ok": False, "error": "websocket not connected"}

                if entity_type == "outputs":
                    if action == "on":
                        ok = await manager.turnOnOutput(entity_id_int)
                        if ok:
                            try:
                                patch = {"ID": str(entity_id_int), "STA": "ON"}
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "off":
                        ok = await manager.turnOffOutput(entity_id_int)
                        if ok:
                            try:
                                patch = {"ID": str(entity_id_int), "STA": "OFF", "LEV": "0"}
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "toggle":
                        try:
                            snap = state.snapshot()
                            ent = next(
                                (
                                    x
                                    for x in (snap.get("entities") or [])
                                    if x.get("type") == "outputs" and int(x.get("id") or -1) == entity_id_int
                                ),
                                None,
                            )
                            rt = (ent or {}).get("realtime") or {}
                            sta_now = str(rt.get("STA") or "").upper()
                        except Exception:
                            sta_now = ""
                        # Ksenia uses ON/OFF for outputs.
                        if sta_now == "ON":
                            ok = await manager.turnOffOutput(entity_id_int)
                            if ok:
                                try:
                                    patch = {"ID": str(entity_id_int), "STA": "OFF", "LEV": "0"}
                                    state.apply_realtime_update("lights", [patch])
                                    publish("lights", patch)
                                    publish("switches", patch)
                                    publish("covers", patch)
                                except Exception:
                                    pass
                            return ok
                        ok = await manager.turnOnOutput(entity_id_int)
                        if ok:
                            try:
                                patch = {"ID": str(entity_id_int), "STA": "ON"}
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "brightness":
                        try:
                            brightness = int(value)
                        except Exception:
                            return False
                        brightness = max(0, min(100, brightness))
                        ok = await manager.turnOnOutput(entity_id_int, brightness=brightness)
                        if ok:
                            try:
                                patch = {"ID": str(entity_id_int), "STA": "ON", "LEV": str(brightness)}
                                state.apply_realtime_update("lights", [patch])
                                publish("lights", patch)
                                publish("switches", patch)
                                publish("covers", patch)
                            except Exception:
                                pass
                        return ok
                    if action == "up":
                        return await manager.raiseCover(entity_id_int)
                    if action == "down":
                        return await manager.lowerCover(entity_id_int)
                    if action == "stop":
                        return await manager.stopCover(entity_id_int)
                    if action == "pos":
                        try:
                            pos = int(value)
                        except Exception:
                            return False
                        pos = max(0, min(100, pos))
                        return await manager.setCoverPosition(entity_id_int, pos)
                    raise ValueError(f"unsupported action for outputs: {action}")

                if entity_type == "scenarios":
                    if action == "execute":
                        if sess:
                            return await sess.execute_scenario(entity_id_int)
                        return await manager.executeScenario(entity_id_int)
                    raise ValueError(f"unsupported action for scenarios: {action}")

                if entity_type == "partitions":
                    if action == "arm":
                        if sess:
                            ok = await sess.set_partition(entity_id_int, "A")
                            if ok:
                                try:
                                    patch = _augment_partition_delay_fields({"ID": str(entity_id_int), "ARM": "DA", "T": "0"})
                                    state.apply_realtime_update("partitions", [patch])
                                    publish("partitions", patch)
                                except Exception:
                                    pass
                            return ok
                        return await manager.armPartition(entity_id_int)
                    if action in ("arm_instant", "arm_now", "instant"):
                        if sess:
                            ok = await sess.set_partition(entity_id_int, "I")
                            if ok:
                                try:
                                    patch = _augment_partition_delay_fields({"ID": str(entity_id_int), "ARM": "IA", "T": "0"})
                                    state.apply_realtime_update("partitions", [patch])
                                    publish("partitions", patch)
                                except Exception:
                                    pass
                            return ok
                        return await manager.armPartitionInstant(entity_id_int)
                    if action in ("arm_delay", "delayed"):
                        if sess:
                            ok = await sess.set_partition(entity_id_int, "A")
                            if ok:
                                try:
                                    patch = _augment_partition_delay_fields({"ID": str(entity_id_int), "ARM": "DA", "T": "0"})
                                    state.apply_realtime_update("partitions", [patch])
                                    publish("partitions", patch)
                                except Exception:
                                    pass
                            return ok
                        return await manager.armPartition(entity_id_int)
                    elif action == "disarm":
                        if sess:
                            ok = await sess.set_partition(entity_id_int, "D")
                            if ok:
                                try:
                                    patch = _augment_partition_delay_fields({"ID": str(entity_id_int), "ARM": "D", "T": "0"})
                                    state.apply_realtime_update("partitions", [patch])
                                    publish("partitions", patch)
                                except Exception:
                                    pass
                            return ok
                        return await manager.disarmPartition(entity_id_int)
                    else:
                        raise ValueError(f"unsupported action for partitions: {action}")

                if entity_type == "zones":
                    if action in ("bypass_on", "byp_on"):
                        ok = await (sess.set_zone_bypass(entity_id_int, "ON") if sess else manager.bypassZoneOn(entity_id_int))
                        if ok:
                            try:
                                patch = {"ID": str(entity_id_int), "BYP": "AUTO"}
                                state.apply_realtime_update("zones", [patch])
                                publish("zones", patch)
                            except Exception:
                                pass
                        return ok
                    if action in ("bypass_off", "byp_off"):
                        ok = await (sess.set_zone_bypass(entity_id_int, "OFF") if sess else manager.bypassZoneOff(entity_id_int))
                        if ok:
                            try:
                                patch = {"ID": str(entity_id_int), "BYP": "NO"}
                                state.apply_realtime_update("zones", [patch])
                                publish("zones", patch)
                            except Exception:
                                pass
                        return ok
                    if action in ("bypass_toggle", "byp_toggle", "bypass_tgl"):
                        ok = await (sess.set_zone_bypass(entity_id_int, "TGL") if sess else manager.bypassZoneToggle(entity_id_int))
                        if ok:
                            try:
                                byp_now = ""
                                snap = state.snapshot()
                                ent = next(
                                    (
                                        x
                                        for x in (snap.get("entities") or [])
                                        if x.get("type") == "zones" and int(x.get("id") or -1) == entity_id_int
                                    ),
                                    None,
                                )
                                rt = (ent or {}).get("realtime") or {}
                                byp_now = str(rt.get("BYP") or "").upper()
                                patch = {"ID": str(entity_id_int), "BYP": ("NO" if byp_now in ("AUTO", "ON", "1") else "AUTO")}
                                state.apply_realtime_update("zones", [patch])
                                publish("zones", patch)
                            except Exception:
                                pass
                        return ok
                    if action in ("bypass", "byp"):
                        byp_val = str(value or "").strip().upper()
                        if byp_val in ("1", "ON", "TRUE"):
                            ok = await (sess.set_zone_bypass(entity_id_int, "ON") if sess else manager.bypassZoneOn(entity_id_int))
                            if ok:
                                try:
                                    patch = {"ID": str(entity_id_int), "BYP": "AUTO"}
                                    state.apply_realtime_update("zones", [patch])
                                    publish("zones", patch)
                                except Exception:
                                    pass
                            return ok
                        if byp_val in ("0", "OFF", "FALSE"):
                            ok = await (sess.set_zone_bypass(entity_id_int, "OFF") if sess else manager.bypassZoneOff(entity_id_int))
                            if ok:
                                try:
                                    patch = {"ID": str(entity_id_int), "BYP": "NO"}
                                    state.apply_realtime_update("zones", [patch])
                                    publish("zones", patch)
                                except Exception:
                                    pass
                            return ok
                        if byp_val in ("-1", "TGL", "TOGGLE"):
                            ok = await (sess.set_zone_bypass(entity_id_int, "TGL") if sess else manager.bypassZoneToggle(entity_id_int))
                            if ok:
                                try:
                                    byp_now = ""
                                    snap = state.snapshot()
                                    ent = next(
                                        (
                                            x
                                            for x in (snap.get("entities") or [])
                                            if x.get("type") == "zones" and int(x.get("id") or -1) == entity_id_int
                                        ),
                                        None,
                                    )
                                    rt = (ent or {}).get("realtime") or {}
                                    byp_now = str(rt.get("BYP") or "").upper()
                                    patch = {"ID": str(entity_id_int), "BYP": ("NO" if byp_now in ("AUTO", "ON", "1") else "AUTO")}
                                    state.apply_realtime_update("zones", [patch])
                                    publish("zones", patch)
                                except Exception:
                                    pass
                            return ok
                        raise ValueError("value must be ON/OFF/TGL (or 1/0/-1)")
                    raise ValueError(f"unsupported action for zones: {action}")

                if entity_type == "accounts":
                    if action in ("enable", "disable"):
                        patch = {
                            "ID": str(entity_id_int),
                            "DACC": "F" if action == "enable" else "T",
                        }
                        ok = await sess.set_account_enabled(patch)
                        if ok:
                            try:
                                state.apply_static_update("accounts", [patch])
                            except Exception:
                                pass
                        return ok
                    if action == "set_enabled":
                        v = str(value or "").strip().upper()
                        if v in ("1", "ON", "TRUE", "T", "ENABLE", "ENABLED"):
                            patch = {"ID": str(entity_id_int), "DACC": "F"}
                        elif v in ("0", "OFF", "FALSE", "F", "DISABLE", "DISABLED"):
                            patch = {"ID": str(entity_id_int), "DACC": "T"}
                        else:
                            raise ValueError("value must be ON/OFF (or 1/0)")
                        ok = await sess.set_account_enabled(patch)
                        if ok:
                            try:
                                state.apply_static_update("accounts", [patch])
                            except Exception:
                                pass
                        return ok
                    raise ValueError(f"unsupported action for accounts: {action}")
                    raise ValueError(f"unsupported action for accounts: {action}")

                if entity_type == "schedulers":
                    if action in ("enable", "disable"):
                        return await manager.updateScheduler(
                            entity_id_int, {"EN": "T" if action == "enable" else "F"}
                        )
                    if action == "set_enabled":
                        v = str(value or "").strip().upper()
                        if v in ("1", "ON", "TRUE", "T"):
                            return await manager.updateScheduler(entity_id_int, {"EN": "T"})
                        if v in ("0", "OFF", "FALSE", "F"):
                            return await manager.updateScheduler(entity_id_int, {"EN": "F"})
                        raise ValueError("value must be ON/OFF (or 1/0)")
                    if action == "set_time":
                        # value can be "HH:MM" or {"H":21,"M":38}
                        if isinstance(value, dict):
                            h = int(value.get("H"))
                            m = int(value.get("M"))
                        else:
                            parts = str(value or "").strip().split(":")
                            if len(parts) != 2:
                                raise ValueError("value must be HH:MM")
                            h = int(parts[0])
                            m = int(parts[1])
                        if not (0 <= h <= 23 and 0 <= m <= 59):
                            raise ValueError("invalid time")
                        return await manager.updateScheduler(entity_id_int, {"H": str(h), "M": str(m)})
                    if action == "set_scenario":
                        sce = int(value)
                        return await manager.updateScheduler(entity_id_int, {"SCE": str(sce)})
                    if action == "set_description":
                        des = str(value or "").strip()
                        if not des:
                            raise ValueError("description cannot be empty")
                        # Web UI writes DES directly.
                        return await manager.updateScheduler(entity_id_int, {"DES": des})
                    if action == "set_excl_holidays":
                        v = str(value or "").strip().upper()
                        if v in ("1", "ON", "TRUE", "T"):
                            return await manager.updateScheduler(entity_id_int, {"EXCL_HOLIDAYS": "T"})
                        if v in ("0", "OFF", "FALSE", "F"):
                            return await manager.updateScheduler(entity_id_int, {"EXCL_HOLIDAYS": "F"})
                        raise ValueError("value must be ON/OFF (or 1/0)")
                    if action == "set_days":
                        # value: {"MON":true,...} or list ["MON","WED"] or "MON,WED"
                        day_keys = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
                        patch = {}
                        if isinstance(value, dict):
                            for k in day_keys:
                                if k in value:
                                    patch[k] = "T" if bool(value.get(k)) else "F"
                        else:
                            if isinstance(value, list):
                                chosen = {str(x).strip().upper() for x in value}
                            else:
                                chosen = {
                                    s.strip().upper()
                                    for s in str(value or "").split(",")
                                    if s.strip()
                                }
                            for k in day_keys:
                                patch[k] = "T" if k in chosen else "F"
                        if not patch:
                            raise ValueError("no day fields provided")
                        return await manager.updateScheduler(entity_id_int, patch)

                    raise ValueError(f"unsupported action for schedulers: {action}")

                if entity_type == "thermostats":
                    if action == "set_description":
                        des = str(value or "").strip()
                        if not des:
                            raise ValueError("description cannot be empty")
                        # Persist thermostat custom names across addon restarts/updates.
                        path = "/data/ui_thermostat_names.json"
                        try:
                            if os.path.exists(path):
                                with open(path, "r", encoding="utf-8") as f:
                                    data = json.load(f)
                            else:
                                data = {}
                        except Exception:
                            data = {}
                        if not isinstance(data, dict):
                            data = {}
                        data[str(entity_id_int)] = des
                        try:
                            os.makedirs(os.path.dirname(path), exist_ok=True)
                            tmp = path + ".tmp"
                            with open(tmp, "w", encoding="utf-8") as f:
                                json.dump(data, f, ensure_ascii=False, indent=2)
                            os.replace(tmp, path)
                        except Exception:
                            pass
                        # Update in-memory snapshot immediately.
                        try:
                            state.apply_static_update("thermostats", [{"ID": str(entity_id_int), "DES": des}])
                        except Exception:
                            pass
                        return {"ok": True}

                    if action == "set_mode":
                        mode = str(value or "").strip().upper()
                        # Observed modes include OFF/MAN/AUTO/WEEKLY and also SD1/SD2.
                        # Keep validation permissive but safe.
                        import re

                        if mode == "AUTO":
                            mode = "WEEKLY"
                        if (not mode) or (re.fullmatch(r"[A-Z0-9_]{1,16}", mode) is None):
                            raise ValueError("invalid mode")
                        return await manager.updateThermostat(
                            entity_id_int, {"ID": str(entity_id_int), "ACT_MODE": mode}
                        )

                    if action == "set_manual_timer":
                        # Manual timed mode uses ACT_MODE=MAN_TMR and duration in MAN_HRS (hours).
                        raw = value
                        if raw in (None, "", "NA"):
                            patch = {"ID": str(entity_id_int), "MAN_HRS": "NA"}
                            return await manager.updateThermostat(entity_id_int, patch)
                        try:
                            hrs = float(str(raw).strip().replace(",", "."))
                        except Exception:
                            raise ValueError("MAN_HRS must be a number (hours) or NA")
                        if hrs < 0:
                            raise ValueError("MAN_HRS must be >= 0")
                        # The panel expects strings (same style as other numeric fields)
                        hrs_s = f"{hrs:.1f}".rstrip("0").rstrip(".")
                        patch = {"ID": str(entity_id_int), "ACT_MODE": "MAN_TMR", "MAN_HRS": hrs_s}
                        return await manager.updateThermostat(entity_id_int, patch)

                    if action == "set_season":
                        season = str(value or "").strip().upper()
                        if season not in ("WIN", "SUM"):
                            raise ValueError("season must be WIN/SUM")
                        return await manager.updateThermostat(entity_id_int, {"ID": str(entity_id_int), "ACT_SEA": season})

                    if action == "set_profile":
                        if not isinstance(value, dict):
                            raise ValueError("value must be an object (dict)")
                        season = str(value.get("season") or "").strip().upper()
                        key = str(value.get("key") or "").strip().upper()
                        raw_val = value.get("value")
                        if season not in ("WIN", "SUM"):
                            # default to current season if not provided
                            season = "WIN"
                            try:
                                snap = state.snapshot()
                                ent = next(
                                    (
                                        x
                                        for x in (snap.get("entities") or [])
                                        if x.get("type") == "thermostats" and int(x.get("id") or -1) == entity_id_int
                                    ),
                                    None,
                                )
                                rt = (ent or {}).get("realtime") or {}
                                therm = rt.get("THERM") if isinstance(rt, dict) else None
                                if isinstance(therm, dict) and therm.get("ACT_SEA"):
                                    season = str(therm.get("ACT_SEA")).strip().upper() or season
                            except Exception:
                                pass
                        if season not in ("WIN", "SUM"):
                            season = "WIN"
                        if key not in ("T1", "T2", "T3", "TM"):
                            raise ValueError("key must be T1/T2/T3/TM")
                        try:
                            v = float(str(raw_val).replace(",", "."))
                        except Exception:
                            raise ValueError("value must be a number")
                        v = max(5.0, min(35.0, v))
                        # Profile thresholds are used in "WEEKLY" mode.
                        patch = {
                            "ID": str(entity_id_int),
                            "ACT_MODE": "WEEKLY",
                            season: {key: f"{v:.1f}"},
                        }
                        return await manager.updateThermostat(entity_id_int, patch)

                    if action == "set_target":
                        try:
                            target = float(str(value).replace(",", "."))
                        except Exception:
                            raise ValueError("target must be a number")
                        target = max(5.0, min(35.0, target))
                        target_str = f"{target:.1f}"

                        season = "WIN"
                        try:
                            snap = state.snapshot()
                            ent = next(
                                (
                                    x
                                    for x in (snap.get("entities") or [])
                                    if x.get("type") == "thermostats" and int(x.get("id") or -1) == entity_id_int
                                ),
                                None,
                            )
                            rt = (ent or {}).get("realtime") or {}
                            st = (ent or {}).get("static") or {}
                            therm = rt.get("THERM") if isinstance(rt, dict) else None
                            if isinstance(therm, dict) and therm.get("ACT_SEA"):
                                season = str(therm.get("ACT_SEA")).strip().upper() or season
                            elif isinstance(st, dict) and st.get("ACT_SEA"):
                                season = str(st.get("ACT_SEA")).strip().upper() or season
                        except Exception:
                            pass
                        if season not in ("WIN", "SUM"):
                            season = "WIN"

                        patch = {
                            "ID": str(entity_id_int),
                            "ACT_MODE": "MAN",
                            "ACT_SEA": season,
                            season: {"TM": target_str},
                        }
                        return await manager.updateThermostat(entity_id_int, patch)

                    if action == "set_schedule":
                        # value: {season:"WIN|SUM", day:"MON|...|SD1|SD2", hour:0-23, t:"1|2|3"}
                        if not isinstance(value, dict):
                            raise ValueError("value must be an object (dict)")
                        season = str(value.get("season") or "").strip().upper()
                        day = str(value.get("day") or "").strip().upper()
                        try:
                            hour = int(value.get("hour"))
                        except Exception:
                            raise ValueError("hour must be 0..23")
                        tval = str(value.get("t") or "").strip()
                        if season not in ("WIN", "SUM"):
                            raise ValueError("season must be WIN/SUM")
                        if day not in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "SD1", "SD2"):
                            raise ValueError("day must be MON..SUN or SD1/SD2")
                        if hour < 0 or hour > 23:
                            raise ValueError("hour must be 0..23")
                        if tval not in ("1", "2", "3"):
                            raise ValueError("t must be 1/2/3")

                        # Read current schedule from debug state snapshot (static cfg).
                        snap = state.snapshot()
                        ent = next(
                            (
                                x
                                for x in (snap.get("entities") or [])
                                if x.get("type") == "thermostats" and int(x.get("id") or -1) == entity_id_int
                            ),
                            None,
                        )
                        st = (ent or {}).get("static") or {}
                        sea_cfg = st.get(season) if isinstance(st, dict) else None
                        cur_day = sea_cfg.get(day) if isinstance(sea_cfg, dict) else None
                        if not isinstance(cur_day, list) or len(cur_day) < 24:
                            raise ValueError(f"schedule {season}.{day} not available")

                        new_day = []
                        for idx in range(24):
                            item = cur_day[idx] if idx < len(cur_day) else None
                            if not isinstance(item, dict):
                                item = {"T": "1", "S": "0"}
                            if idx == hour:
                                new_day.append({**item, "T": tval})
                            else:
                                new_day.append(dict(item))

                        patch = {"ID": str(entity_id_int), season: {day: new_day}}
                        return await manager.updateThermostat(entity_id_int, patch)

                    if action == "write_patch":
                        if not isinstance(value, dict):
                            raise ValueError("value must be an object (dict)")
                        patch = dict(value)
                        patch["ID"] = str(entity_id_int)
                        return await manager.updateThermostat(entity_id_int, patch)

                    raise ValueError(f"unsupported action for thermostats: {action}")

                if entity_type in ("system", "panel"):
                    if action == "clear_cycles_or_memories":
                        return await manager.clearPanel("CYCLES_OR_MEMORIES")
                    if action == "clear_communications":
                        return await manager.clearPanel("COMMUNICATIONS")
                    if action == "clear_faults_memory":
                        return await manager.clearPanel("FAULTS_MEMORY")
                    raise ValueError(f"unsupported action for system: {action}")

                raise ValueError(f"unsupported type: {entity_type}")

            fut = asyncio.run_coroutine_threadsafe(_coro(), loop)
            try:
                result = fut.result(timeout=20)
            except concurrent.futures.TimeoutError:
                return {"ok": False, "error": "timeout"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

            # If the coroutine returns a structured response (e.g. pin_session_required),
            # pass it through verbatim. Otherwise, coerce truthiness to ok.
            if isinstance(result, dict) and "ok" in result:
                return result
            return {"ok": bool(result)}

        set_command_handler(_execute_command)

        logger.info(f"Ksenia: {ksenia_host}:{ksenia_port}")
        logger.info(f"MQTT: {mqtt_host}:{mqtt_port} prefix={mqtt_prefix}")

        asyncio.create_task(_ws1_status_poller())

        await manager.connect()
        if not getattr(manager, "_running", False):
            logger.info("Connessione WS non riuscita, provo wss...")
            await manager.connectSecure()

        await manager.wait_for_initial_data(timeout=30)

        try:
            state.set_initial_data(getattr(manager, "_readData", None), getattr(manager, "_realtimeInitialData", None))
        except Exception as exc:
            logger.error(f"Debug initial ingest error: {exc}")
        try:
            publish_discovery(state.snapshot())
            try:
                _seed_partition_states(state.snapshot())
            except Exception:
                pass
            try:
                _seed_output_states(state.snapshot())
            except Exception:
                pass
        except Exception as exc:
            logger.error(f"Discovery publish error: {exc}")
        try:
            if icon_notifier.enabled():
                icon_notifier.maybe_notify(state.snapshot())
        except Exception as exc:
            logger.error(f"Icon HTTP notify error: {exc}")

        # Enrich systems with panel model/version info (SYSTEM_VERSION).
        try:
            sv = getattr(manager, "_systemVersion", None) or {}
            if isinstance(sv, dict) and sv:
                sys_id = int((sv.get("ID") or 1))
                enrich = {"ID": sys_id}
                for k in ("MODEL", "BRAND", "BOOT", "IP", "FS", "SSL", "OS", "VER_LITE", "PRG_CHECK"):
                    if k in sv:
                        enrich[k] = sv.get(k)
                state.apply_static_update("systems", [enrich])
        except Exception as exc:
            logger.error(f"Errore leggendo SYSTEM_VERSION: {exc}")

        try:
            for item in await manager.getLights():
                publish("lights", item)
            for item in await manager.getRolls():
                publish("covers", item)
            for item in await manager.getSwitches():
                publish("switches", item)
            for item in await manager.getDom():
                publish("domus", item)
            for item in await manager.getThermostats():
                publish("thermostats", item)
            scheds = await manager.getSchedulers()
            if isinstance(scheds, list):
                try:
                    state.apply_static_update("schedulers", scheds)
                except Exception as exc:
                    logger.error(f"Debug schedulers ingest error: {exc}")
                for item in scheds:
                    if isinstance(item, dict):
                        publish("schedulers", item)
            for item in await manager.getSystem():
                publish("systems", item)
            logs_state = getattr(manager, "_logs_state", None) or {}
            logs_list = logs_state.get("LOGS") if isinstance(logs_state, dict) else None
            if isinstance(logs_list, list):
                try:
                    state.apply_realtime_update("logs", logs_list)
                except Exception as exc:
                    logger.error(f"Debug logs ingest error: {exc}")
                for item in logs_list:
                    if isinstance(item, dict):
                        publish("logs", item)
            conn_list = (
                (getattr(manager, "_realtimeInitialData", None) or {})
                .get("PAYLOAD", {})
                .get("STATUS_CONNECTION", [])
                or []
            )
            for item in conn_list:
                if isinstance(item, dict):
                    publish("connection", item)
                    gsm = _gsm_from_connection_item(item)
                    if gsm:
                        publish("gsm", gsm)
        except Exception as exc:
            logger.error(f"Errore pubblicando stato iniziale: {exc}")

        await asyncio.Event().wait()

    asyncio.run(run())

if __name__ == "__main__":
    main()



