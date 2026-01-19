"""
Huge thanks to @realnot16 for these functions!
"""

import asyncio
import websockets
import json, time
import ssl
import copy
import inspect
from wscall import (
    ws_login,
    realtime,
    readData,
    readZones,
    exeScenario,
    setOutput,
    armPartition,
    bypZone,
    systemVersion,
    getLogs,
    readSchedulers,
    readThermostatsCfg,
    writeCfg,
    writeCfgTyped,
    clearCmd,
)

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
ssl_context.verify_mode = ssl.CERT_NONE
ssl_context.options |= 0x4


class WebSocketManager:
    """
    Constructor for WebSocketManager

    :param ip: IP of the Ksenia Lares panel
    :param pin: PIN to login to the Ksenia Lares panel
    :param logger: Logger instance
    """

    def __init__(
        self,
        ip,
        pin,
        port,
        logger,
        debug_thermostats: bool = False,
        output_debug_verbose: bool = False,
        reconnect_cooldown_sec: int | float = 8,
    ):
        self._ip = ip
        self._port = port
        self._pin = pin
        self._ws = None
        self.listeners = {
            "lights": [],
            "covers": [],
            "domus": [],
            "switches": [],
            "powerlines": [],
            "partitions": [],
            "zones": [],
            "systems": [],
            "connection": [],
            "thermostats": [],
            "thermostats_cfg": [],
            "logs": [],
            "schedulers": [],
        }
        self._logger = logger
        self._debug_thermostats = bool(debug_thermostats)
        self._output_debug_verbose = bool(output_debug_verbose)
        try:
            self._reconnect_cooldown_sec = max(0.0, float(reconnect_cooldown_sec))
        except Exception:
            self._reconnect_cooldown_sec = 8.0
        self._running = False  # Flag to keep process alive
        self._loginId = None
        self._realtimeInitialData = None  # Initial realtime data
        self._readData = None  # Static data read
        self._systemVersion = None  # SYSTEM_VERSION payload
        self._logs_state = None  # last logs payload (LOGS_RES PAYLOAD)
        self._logs_last_id = None
        self._schedulers_state = None  # last scheduler payload (READ_RES PAYLOAD)
        self._ws_lock = asyncio.Lock()
        self._command_queue = asyncio.Queue()  # Command queue
        self._pending_commands = {}
        self._listener_task = None
        self._cmd_task = None
        self._reconnect_task = None
        self._logs_task = None
        self._schedulers_task = None
        self._thermostats_task = None
        self._zones_task = None
        self._last_zones_by_id = {}
        self._on_reconnect = None  # async callback(read_data, realtime_initial, system_version)

        self._max_retries = 20
        self._retry_delay = 2
        self._retries = 0
        self._max_retry_delay = 60
        self._connSecure = 0  # 0: no SSL, 1: SSL
        self._last_thermo_cfg_by_id = {}
        self._cooldown_until = 0.0

    async def _notify_listeners(self, entity_type: str, payload):
        callbacks = self.listeners.get(entity_type, [])
        if not callbacks:
            return
        for cb in list(callbacks):
            try:
                res = cb(payload)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                self._logger.error("listener %s callback error: %s", entity_type, exc)

    def _thermo_cfg_compact(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        out = {
            "ID": str(item.get("ID") or ""),
            "ACT_MODE": item.get("ACT_MODE"),
            "ACT_SEA": item.get("ACT_SEA"),
            "MAN_HRS": item.get("MAN_HRS"),
        }
        for season in ("WIN", "SUM"):
            s = item.get(season)
            if isinstance(s, dict):
                out[season] = {k: s.get(k) for k in ("T1", "T2", "T3", "TM")}
        tof = item.get("TOF")
        if isinstance(tof, dict):
            out["TOF"] = {k: tof.get(k) for k in ("T", "E")}
        return out

    def set_on_reconnect(self, callback):
        """
        Register an async callback called after (re)connect completes initial sync.

        Signature: await callback(read_data, realtime_initial, system_version)
        """
        self._on_reconnect = callback

    async def _call_on_reconnect(self):
        cb = self._on_reconnect
        if cb is None:
            return
        try:
            await cb(self._readData, self._realtimeInitialData, self._systemVersion)
        except Exception as exc:
            self._logger.error("on_reconnect callback failed: %s", exc)

    def _ensure_background_tasks(self):
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self.listener())
        if self._cmd_task is None or self._cmd_task.done():
            self._cmd_task = asyncio.create_task(self.process_command_queue())
        if self._logs_task is None or self._logs_task.done():
            self._logs_task = asyncio.create_task(self.logs_poller())
        if self._schedulers_task is None or self._schedulers_task.done():
            self._schedulers_task = asyncio.create_task(self.schedulers_poller())
        if self._thermostats_task is None or self._thermostats_task.done():
            self._thermostats_task = asyncio.create_task(self.thermostats_cfg_poller())
        if self._zones_task is None or self._zones_task.done():
            self._zones_task = asyncio.create_task(self.zones_poller())

    def _zone_compact(self, z: dict) -> dict:
        if not isinstance(z, dict):
            return {}
        # Keys we care about for HA extra sensors / bypass.
        keys = ("STA", "BYP", "T", "AN", "A", "FM", "VAS", "OHM", "CMD", "BYP_EN")
        out = {"ID": str(z.get("ID") or "")}
        for k in keys:
            if k in z:
                out[k] = z.get(k)
        return out

    async def fetch_logs(self, items=150):
        if not self._ws or not self._loginId:
            return []
        async with self._ws_lock:
            payload = await getLogs(
                self._ws,
                self._loginId,
                self._logger,
                items=items,
                dispatch_unhandled=self.handle_message,
            )
        if not isinstance(payload, dict):
            return []
        logs = payload.get("LOGS") or []
        if not isinstance(logs, list):
            return []
        # Normalize/track last id.
        try:
            max_id = max(int(str(x.get("ID"))) for x in logs if isinstance(x, dict) and x.get("ID") is not None)
        except Exception:
            max_id = None
        self._logs_state = payload
        if max_id is not None:
            self._logs_last_id = max_id
        return logs

    async def logs_poller(self):
        # Periodically fetch logs; emit only new entries.
        items = 500
        poll_s = 5.0
        while True:
            if not self._running:
                await asyncio.sleep(1.0)
                continue
            try:
                async with self._ws_lock:
                    if not self._ws or not self._loginId:
                        await asyncio.sleep(poll_s)
                        continue
                    payload = await getLogs(
                        self._ws,
                        self._loginId,
                        self._logger,
                        items=items,
                        dispatch_unhandled=self.handle_message,
                    )
                logs = (payload or {}).get("LOGS") or []
                if not isinstance(logs, list):
                    await asyncio.sleep(poll_s)
                    continue

                # Determine new entries by monotonically increasing ID.
                new_entries = []
                last_id = self._logs_last_id
                ids_in_window = []
                for item in logs:
                    if not isinstance(item, dict):
                        continue
                    try:
                        ids_in_window.append(int(str(item.get("ID"))))
                    except Exception:
                        continue
                ids_in_window = [x for x in ids_in_window if x is not None]
                if last_id is not None and ids_in_window:
                    max_id = max(ids_in_window)
                    min_id = min(ids_in_window)
                    if max_id - last_id >= items and last_id < min_id:
                        self._logger.warning(
                            "LOGS window too small: last_id=%s, fetched range=%s..%s (items=%s) -> some events may be missed",
                            last_id,
                            min_id,
                            max_id,
                            items,
                        )
                for item in logs:
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_id = int(str(item.get("ID")))
                    except Exception:
                        continue
                    if last_id is None or item_id > last_id:
                        new_entries.append(item)
                if logs:
                    try:
                        self._logs_last_id = max(
                            int(str(x.get("ID"))) for x in logs if isinstance(x, dict) and x.get("ID") is not None
                        )
                    except Exception:
                        pass
                self._logs_state = payload

                if new_entries:
                    # Sort ascending for consumers.
                    try:
                        new_entries.sort(key=lambda x: int(str(x.get("ID") or 0)))
                    except Exception:
                        pass
                    await self._notify_listeners("logs", new_entries)
            except Exception as exc:
                self._logger.error("logs poller error: %s", exc)
            await asyncio.sleep(poll_s)

    async def schedulers_poller(self):
        # Periodically refresh scheduler configuration (it may change from web UI).
        poll_s = 30.0
        while True:
            if not self._running:
                await asyncio.sleep(2.0)
                continue
            try:
                async with self._ws_lock:
                    if not self._ws or not self._loginId:
                        await asyncio.sleep(poll_s)
                        continue
                    payload = await readSchedulers(
                        self._ws,
                        self._loginId,
                        self._logger,
                        dispatch_unhandled=self.handle_message,
                    )
                if not isinstance(payload, dict):
                    await asyncio.sleep(poll_s)
                    continue
                timers = payload.get("CFG_SCHEDULER_TIMERS")
                holidays = payload.get("CFG_HOLIDAYS")
                if timers is None and holidays is None:
                    await asyncio.sleep(poll_s)
                    continue
                self._schedulers_state = payload
                if self._readData is None:
                    self._readData = {}
                if isinstance(timers, list):
                    self._readData["CFG_SCHEDULER_TIMERS"] = timers
                if isinstance(holidays, list):
                    self._readData["CFG_HOLIDAYS"] = holidays
                if isinstance(timers, list):
                    await self._notify_listeners("schedulers", timers)
            except Exception as exc:
                self._logger.error("schedulers poller error: %s", exc)
            await asyncio.sleep(poll_s)

    async def thermostats_cfg_poller(self):
        # Periodically refresh full thermostat configuration (it changes outside our add-on, and isn't pushed in realtime).
        poll_s = 15.0
        while True:
            if not self._running:
                await asyncio.sleep(2.0)
                continue
            try:
                async with self._ws_lock:
                    if not self._ws or not self._loginId:
                        await asyncio.sleep(poll_s)
                        continue
                    payload = await readThermostatsCfg(
                        self._ws,
                        self._loginId,
                        self._logger,
                        pin=self._pin,
                        dispatch_unhandled=self.handle_message,
                    )
                if not isinstance(payload, dict):
                    await asyncio.sleep(poll_s)
                    continue
                cfg_list = payload.get("CFG_THERMOSTATS")
                if not isinstance(cfg_list, list):
                    await asyncio.sleep(poll_s)
                    continue
                if self._readData is None:
                    self._readData = {}
                self._readData["CFG_THERMOSTATS"] = cfg_list
                if self._debug_thermostats:
                    try:
                        new_by_id = {str(x.get("ID")): self._thermo_cfg_compact(x) for x in cfg_list if isinstance(x, dict)}
                        old_by_id = self._last_thermo_cfg_by_id or {}
                        for tid, cur in new_by_id.items():
                            prev = old_by_id.get(tid)
                            if prev is None:
                                self._logger.info("THERMO CFG new ID=%s -> %s", tid, cur)
                                continue
                            if cur != prev:
                                self._logger.info("THERMO CFG change ID=%s: %s -> %s", tid, prev, cur)
                        self._last_thermo_cfg_by_id = new_by_id
                    except Exception as exc:
                        self._logger.error("THERMO cfg debug diff failed: %s", exc)
                await self._notify_listeners("thermostats_cfg", cfg_list)
            except Exception as exc:
                self._logger.error("thermostats cfg poller error: %s", exc)
            await asyncio.sleep(poll_s)

    async def zones_poller(self):
        # Periodically refresh zones snapshot because some panels don't push all fields
        # (e.g. BYP/tamper/mask) via realtime STATUS_ZONES.
        poll_s = 5.0
        while True:
            if not self._running:
                await asyncio.sleep(2.0)
                continue
            try:
                async with self._ws_lock:
                    if not self._ws or not self._loginId:
                        await asyncio.sleep(poll_s)
                        continue
                    payload = await readZones(
                        self._ws,
                        self._loginId,
                        self._logger,
                        dispatch_unhandled=self.handle_message,
                    )
                zones = (payload or {}).get("ZONES")
                if not isinstance(zones, list):
                    await asyncio.sleep(poll_s)
                    continue

                if self._readData is None:
                    self._readData = {}
                self._readData["ZONES"] = zones

                new_by_id = {}
                updates = []
                for z in zones:
                    if not isinstance(z, dict):
                        continue
                    zid = str(z.get("ID") or "")
                    if not zid:
                        continue
                    compact = self._zone_compact(z)
                    new_by_id[zid] = compact
                    prev = self._last_zones_by_id.get(zid)
                    if prev is None:
                        updates.append(compact)
                        continue
                    if compact != prev:
                        # Send only changed keys to keep MQTT churn low.
                        patch = {"ID": zid}
                        for k, v in compact.items():
                            if k == "ID":
                                continue
                            if prev.get(k) != v:
                                patch[k] = v
                        if len(patch) > 1:
                            updates.append(patch)

                self._last_zones_by_id = new_by_id
                if updates:
                    await self._notify_listeners("zones", updates)
            except Exception as exc:
                self._logger.error("zones poller error: %s", exc)
            await asyncio.sleep(poll_s)

    async def _close_ws(self):
        # Serialize close/reset with other ws operations.
        try:
            async with self._ws_lock:
                try:
                    if self._ws is not None:
                        await self._ws.close()
                except Exception:
                    pass
                self._ws = None
                self._loginId = None
        except Exception:
            # If the lock itself is broken, still clear state to avoid NoneType.send races.
            self._ws = None
            self._loginId = None

    async def _reconnect_forever(self):
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return

        async def _runner():
            self._logger.info("Reconnect loop started")
            self._retries = 0
            self._retry_delay = 2
            while True:
                try:
                    if self._connSecure:
                        await self.connectSecure()
                    else:
                        await self.connect()
                    if getattr(self, "_running", False):
                        return
                except Exception as exc:
                    self._logger.error("Reconnect attempt failed: %s", exc)
                # If the panel is rebooting / applying config, it may reject connections (HTTP 500)
                # for a while. Back off aggressively to avoid hammering it.
                try:
                    await asyncio.sleep(min(self._retry_delay, self._max_retry_delay))
                except Exception:
                    await asyncio.sleep(2.0)
                self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

        self._reconnect_task = asyncio.create_task(_runner())

    """
    Wait until the initial data are available.

    The method waits until the initial data (both static and realtime) are
    available. If the data are not available within the given timeout, it
    raises an exception.

    :param timeout: The timeout in seconds to wait for the data
    :raises TimeoutError: if the data are not available within the timeout
    """

    async def wait_for_initial_data(self, timeout=10):
        start_time = time.time()
        while (self._readData is None or self._realtimeInitialData is None) and (
            time.time() - start_time < timeout
        ):
            await asyncio.sleep(0.5)

    """
    Establishes a NOT SECURE connection to the Ksenia Lares web socket server.

    This method is responsible for establishing a connection to the Ksenia Lares
    web socket server and retrieving the initial data (both static and
    realtime). If the connection fails, it retries up to a maximum number of
    times. If all retries fail, the method raises an exception.

    :raises websockets.exceptions.WebSocketException: if the connection fails
    :raises OSError: if the connection fails
    """

    async def connect(self):
        # Reset retry counters for this connect attempt.
        self._retries = 0
        self._retry_delay = 2
        self._connSecure = 0
        await self._close_ws()
        while self._retries < self._max_retries:
            try:
                # Respect cooldown (some panels reject reconnect for a while after "Bye...").
                try:
                    now = time.time()
                    if float(self._cooldown_until or 0.0) > now:
                        await asyncio.sleep(max(1.0, float(self._cooldown_until) - now))
                except Exception:
                    pass
                try:
                    pin_len = len(str(self._pin or ""))
                    self._logger.info(f"WS1 connecting (ws) pin length={pin_len}")
                except Exception:
                    pass
                uri = f"ws://{self._ip}:{self._port}/KseniaWsock"
                self._logger.info("Connecting to WebSocket...")
                self._ws = await websockets.connect(uri, subprotocols=["KS_WSOCK"])
                self._loginId = await ws_login(self._ws, self._pin, self._logger)
                if self._loginId < 0:
                    self._logger.error("WebSocket login error, retrying...")
                    await self._close_ws()
                    self._retries += 1
                    await asyncio.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)
                    continue
                self._logger.info(f"Connected to websocket - ID {self._loginId}")
                try:
                    pin_len = len(str(self._pin or ""))
                    self._logger.info(f"WS1 login OK (ws) login_id={self._loginId} pin length={pin_len}")
                except Exception:
                    pass
                self._logger.info("Reading system version")
                self._systemVersion = await systemVersion(self._ws, self._loginId, self._logger)
                self._logger.info("Reading event logs")
                self._logs_state = await getLogs(self._ws, self._loginId, self._logger, items=500)
                try:
                    logs = (self._logs_state or {}).get("LOGS") or []
                    if isinstance(logs, list) and logs:
                        self._logs_last_id = max(int(str(x.get("ID"))) for x in logs if isinstance(x, dict) and x.get("ID") is not None)
                except Exception:
                    pass
                self._logger.info("Extracting initial data")
                self._readData = await readData(self._ws, self._loginId, self._logger)
                # Fetch full thermostat config (mobile app uses a dedicated PAYLOAD_TYPE and returns full schedules).
                try:
                    self._logger.info("Reading thermostat config (CFG_THERMOSTATS)")
                    therm_payload = await readThermostatsCfg(
                        self._ws,
                        self._loginId,
                        self._logger,
                        pin=self._pin,
                        dispatch_unhandled=self.handle_message,
                    )
                    cfg_list = therm_payload.get("CFG_THERMOSTATS") if isinstance(therm_payload, dict) else None
                    if isinstance(cfg_list, list):
                        if self._readData is None:
                            self._readData = {}
                        self._readData["CFG_THERMOSTATS"] = cfg_list
                except Exception as exc:
                    self._logger.error("Thermostat config read failed: %s", exc)
                self._logger.info("Realtime connection started")
                self._realtimeInitialData = await realtime(self._ws, self._loginId, self._logger)
                self._logger.debug("Initial data acquired")
                self._running = True
                self._retries = 0
                self._retry_delay = 2
                self._ensure_background_tasks()
                await self._call_on_reconnect()
                return
            except (websockets.exceptions.WebSocketException, OSError) as e:
                self._logger.error(
                    f"WebSocket connection failed: {e}. Retrying in {self._retry_delay} seconds..."
                )
                await asyncio.sleep(self._retry_delay)
                self._retries += 1
                self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

        self._logger.critical("Maximum retries reached. WebSocket connection failed.")
        raise ConnectionError("ws connect max retries reached")

    """
    Establishes a SECURE connection to the Ksenia Lares web socket server.

    This method is responsible for establishing a connection to the Ksenia Lares
    web socket server and retrieving the initial data (both static and
    realtime). If the connection fails, it retries up to a maximum number of
    times. If all retries fail, the method raises an exception.

    :raises websockets.exceptions.WebSocketException: if the connection fails
    :raises OSError: if the connection fails
    """

    async def connectSecure(self):
        # Reset retry counters for this connect attempt.
        self._retries = 0
        self._retry_delay = 2
        self._connSecure = 1
        await self._close_ws()
        while self._retries < self._max_retries:
            try:
                # Respect cooldown (some panels reject reconnect for a while after "Bye...").
                try:
                    now = time.time()
                    if float(self._cooldown_until or 0.0) > now:
                        await asyncio.sleep(max(1.0, float(self._cooldown_until) - now))
                except Exception:
                    pass
                try:
                    pin_len = len(str(self._pin or ""))
                    self._logger.info(f"WS1 connecting (wss) pin length={pin_len}")
                except Exception:
                    pass
                uri = f"wss://{self._ip}:{self._port}/KseniaWsock"
                self._logger.info(f"Connecting to WebSocket... {uri}")
                self._ws = await websockets.connect(
                    uri, ssl=ssl_context, subprotocols=["KS_WSOCK"]
                )
                self._loginId = await ws_login(self._ws, self._pin, self._logger)
                if self._loginId < 0:
                    self._logger.error("WebSocket login error, retrying...")
                    await self._close_ws()
                    self._retries += 1
                    await asyncio.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)
                    continue
                self._logger.info(f"Connected to websocket - ID {self._loginId}")
                try:
                    pin_len = len(str(self._pin or ""))
                    self._logger.info(f"WS1 login OK (wss) login_id={self._loginId} pin length={pin_len}")
                except Exception:
                    pass
                self._logger.info("Reading system version")
                self._systemVersion = await systemVersion(self._ws, self._loginId, self._logger)
                self._logger.info("Reading event logs")
                self._logs_state = await getLogs(self._ws, self._loginId, self._logger, items=500)
                try:
                    logs = (self._logs_state or {}).get("LOGS") or []
                    if isinstance(logs, list) and logs:
                        self._logs_last_id = max(int(str(x.get("ID"))) for x in logs if isinstance(x, dict) and x.get("ID") is not None)
                except Exception:
                    pass
                self._logger.info("Extracting initial data")
                self._readData = await readData(self._ws, self._loginId, self._logger)
                # Fetch full thermostat config (mobile app uses a dedicated PAYLOAD_TYPE and returns full schedules).
                try:
                    self._logger.info("Reading thermostat config (CFG_THERMOSTATS)")
                    therm_payload = await readThermostatsCfg(
                        self._ws,
                        self._loginId,
                        self._logger,
                        pin=self._pin,
                        dispatch_unhandled=self.handle_message,
                    )
                    cfg_list = therm_payload.get("CFG_THERMOSTATS") if isinstance(therm_payload, dict) else None
                    if isinstance(cfg_list, list):
                        if self._readData is None:
                            self._readData = {}
                        self._readData["CFG_THERMOSTATS"] = cfg_list
                except Exception as exc:
                    self._logger.error("Thermostat config read failed: %s", exc)
                self._logger.info("Realtime connection started")
                self._realtimeInitialData = await realtime(self._ws, self._loginId, self._logger)
                self._logger.info("Initial data acquired")
                self._running = True
                self._retries = 0
                self._retry_delay = 2
                self._ensure_background_tasks()
                await self._call_on_reconnect()
                return
            except (websockets.exceptions.WebSocketException, OSError) as e:
                self._logger.error(
                    f"WebSocket connection failed: {e}. Retrying in {self._retry_delay} seconds..."
                )
                await asyncio.sleep(self._retry_delay)
                self._retries += 1
                self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

        self._logger.critical("Maximum retries reached. WebSocket connection failed.")
        raise ConnectionError("wss connect max retries reached")

    """
    Listener for the websocket messages.

    This method is responsible for listening for messages from the Ksenia
    Lares web socket server. If a message is received, it is decoded as JSON
    and passed to the handle_message method for further processing. If the
    connection is closed, it tries to reconnect up to a maximum number of
    times.

    :raises Exception: if the connection is closed or an error occurs
    """

    async def listener(self):
        self._logger.info("Starting listener")
        last_msg = time.time()
        while self._running:
            message = None
            try:
                async with self._ws_lock:
                    message = await asyncio.wait_for(self._ws.recv(), timeout=3)
                    last_msg = time.time()
            except asyncio.TimeoutError:
                # Keepalive: if we haven't received data for a while, ping to detect dead sockets.
                if time.time() - last_msg > 20:
                    try:
                        async with self._ws_lock:
                            pong_waiter = await self._ws.ping()
                            await asyncio.wait_for(pong_waiter, timeout=5)
                            last_msg = time.time()
                    except Exception:
                        self._logger.error("WebSocket ping failed, reconnecting...")
                        self._running = False
                        await self._close_ws()
                        await self._reconnect_forever()
                        return
                continue
            except websockets.exceptions.ConnectionClosed as exc:
                try:
                    self._logger.error(
                        "WebSocket closed (code=%s reason=%s), reconnecting...",
                        getattr(exc, "code", None),
                        getattr(exc, "reason", None),
                    )
                except Exception:
                    self._logger.error("WebSocket closed, reconnecting...")
                self._running = False
                await self._close_ws()
                # Some panels reject reconnect for a while after a clean close (code 1000 "Bye...").
                try:
                    if int(getattr(exc, "code", 0) or 0) == 1000:
                        self._cooldown_until = max(
                            float(self._cooldown_until or 0.0),
                            time.time() + float(self._reconnect_cooldown_sec or 0.0),
                        )
                except Exception:
                    pass
                await self._reconnect_forever()
                return
            except Exception as e:
                self._logger.error(f"Listener error: {e}")
                continue

            if message:
                try:
                    data = json.loads(message)
                except Exception as e:
                    self._logger.error(f"Error decoding JSON: {e}")
                    continue
                try:
                    await self.handle_message(data)
                except Exception as exc:
                    # Don't let malformed/unexpected payloads kill the listener task
                    # (otherwise the add-on appears "stuck" until restart).
                    try:
                        self._logger.error("handle_message error: %s", exc)
                    except Exception:
                        pass
                    continue

    """
    Handles messages received from the Ksenia Lares WebSocket server.

    This method is called whenever a message is received from the WebSocket
    server. It checks the type of the message and processes it accordingly.
    If the message is a result of a command (CMD_USR_RES), it checks if the
    command is present in the pending commands dictionary, and if so,
    resolves the associated future with a successful result. If the message
    is a real-time update (REALTIME), it checks the type of data contained
    in the message and updates the corresponding real-time data dictionary.
    It also notifies the registered listeners for the corresponding type of
    data.

    :param message: the message received from the WebSocket server
    :type message: dict
    """

    async def handle_message(self, message):
        payload = message.get("PAYLOAD", {})
        if "HomeAssistant" in payload:
            data = payload["HomeAssistant"]
        else:
            # payload like { "<receiver_id>": { ƒ?İ } }
            data = next(iter(payload.values()), {})
        if not isinstance(data, dict):
            data = {}

        if message.get("CMD") == "CMD_USR_RES":
            if not self._pending_commands:
                self._logger.warning("Received CMD_USR_RES but no commands were pending")
                return

            command_id = str(message.get("ID") or "")
            command_data = self._pending_commands.get(command_id)

            def _extract_result() -> str | None:
                try:
                    pay = message.get("PAYLOAD")
                    if isinstance(pay, dict):
                        # Some panels include RESULT at the root of PAYLOAD.
                        if "RESULT" in pay:
                            return str(pay.get("RESULT"))
                        # Most include RESULT inside the receiver object (often HomeAssistant).
                        if isinstance(data, dict) and "RESULT" in data:
                            return str(data.get("RESULT"))
                        # Last resort: scan nested dicts.
                        for v in pay.values():
                            if isinstance(v, dict) and "RESULT" in v:
                                return str(v.get("RESULT"))
                except Exception:
                    return None
                return None

            def _is_security_cmd(cmd_data: dict | None) -> bool:
                try:
                    if not isinstance(cmd_data, dict):
                        return False
                    cmd = cmd_data.get("command")
                    if cmd == "SCENARIO":
                        return True
                    if isinstance(cmd, dict) and cmd.get("type") in ("PARTITION", "BYPASS"):
                        return True
                except Exception:
                    return False
                return False

            result_raw = _extract_result()
            if result_raw is None:
                # If RESULT is missing, be conservative for security commands to avoid "OK" fakes.
                result_ok = False if _is_security_cmd(command_data) else True
            else:
                result_ok = str(result_raw).strip().upper() == "OK"

            if command_data:
                try:
                    command_data["future"].set_result(bool(result_ok))
                except Exception:
                    pass
                self._pending_commands.pop(command_id, None)
                return

            # Some panels reply with an unexpected/mismatched ID. If we only have one
            # pending command, assume it's the one being acknowledged.
            if len(self._pending_commands) == 1:
                only_id, only_data = next(iter(self._pending_commands.items()))
                self._logger.warning(
                    "CMD_USR_RES id mismatch (got %s, pending %s) - accepting reply",
                    command_id,
                    only_id,
                )
                try:
                    only_data["future"].set_result(bool(result_ok))
                except Exception:
                    pass
                self._pending_commands.pop(only_id, None)
                return

            self._logger.warning(
                "CMD_USR_RES id %s not found (%s pending)",
                command_id,
                len(self._pending_commands),
            )

        elif message.get("CMD") == "REALTIME":
            if "STATUS_OUTPUTS" in data:
                updates_raw = data.get("STATUS_OUTPUTS")
                # Some panels send a single object instead of a list for realtime updates.
                # Normalize so downstream (state/UI/MQTT) always sees a list.
                if isinstance(updates_raw, dict):
                    updates = [updates_raw]
                elif isinstance(updates_raw, list):
                    updates = updates_raw
                else:
                    updates = []

                self._logger.debug(f"Updating state for outputs: {updates_raw}")
                if self._output_debug_verbose:
                    try:
                        if isinstance(updates, list):
                            sample = []
                            for it in updates[:12]:
                                if not isinstance(it, dict):
                                    continue
                                row = {"ID": it.get("ID")}
                                for k in ("STA", "LEV", "DIM", "POS", "TYP", "CAT"):
                                    if k in it:
                                        row[k] = it.get(k)
                                sample.append(row)
                            self._logger.info(
                                "WS1 REALTIME STATUS_OUTPUTS n=%s sample=%s",
                                len(updates),
                                sample,
                            )
                        else:
                            self._logger.info(
                                "WS1 REALTIME STATUS_OUTPUTS type=%s",
                                type(updates_raw).__name__,
                            )
                    except Exception:
                        pass

                if updates:
                    # Update the initial realtime snapshot (merge-by-ID, to tolerate partial updates).
                    if self._realtimeInitialData is None:
                        self._realtimeInitialData = {}
                    if "PAYLOAD" not in self._realtimeInitialData:
                        self._realtimeInitialData["PAYLOAD"] = {}
                    try:
                        prev = self._realtimeInitialData["PAYLOAD"].get("STATUS_OUTPUTS") or []
                        if isinstance(prev, dict):
                            prev = [prev]
                        prev_by_id = {}
                        if isinstance(prev, list):
                            for it in prev:
                                if not isinstance(it, dict):
                                    continue
                                prev_by_id[str(it.get("ID"))] = it
                        for it in updates:
                            if not isinstance(it, dict):
                                continue
                            oid = str(it.get("ID"))
                            if oid in prev_by_id and isinstance(prev_by_id.get(oid), dict):
                                prev_by_id[oid] = {**prev_by_id[oid], **it}
                            else:
                                prev_by_id[oid] = it
                        self._realtimeInitialData["PAYLOAD"]["STATUS_OUTPUTS"] = list(
                            prev_by_id.values()
                        )
                    except Exception:
                        self._realtimeInitialData["PAYLOAD"]["STATUS_OUTPUTS"] = updates

                # Notify listeners
                await self._notify_listeners("lights", updates)
                await self._notify_listeners("switches", updates)
                await self._notify_listeners("covers", updates)
            if "STATUS_BUS_HA_SENSORS" in data:
                domus_updates = data["STATUS_BUS_HA_SENSORS"]
                self._logger.debug(
                    "DOMUS realtime update (raw STATUS_BUS_HA_SENSORS): %s",
                    domus_updates,
                )

                # Update the initial realtime DOMUS data so getDom() sees fresh values
                if self._realtimeInitialData is None:
                    self._realtimeInitialData = {}
                if "PAYLOAD" not in self._realtimeInitialData:
                    self._realtimeInitialData["PAYLOAD"] = {}
                self._realtimeInitialData["PAYLOAD"]["STATUS_BUS_HA_SENSORS"] = (
                    domus_updates
                )

                # Notify listeners (entities registered under 'domus')
                await self._notify_listeners("domus", domus_updates)
            if "STATUS_POWER_LINES" in data:
                await self._notify_listeners("powerlines", data["STATUS_POWER_LINES"])
            if "STATUS_PARTITIONS" in data:
                self._logger.debug(
                    "Realtime partitions update: %s", data["STATUS_PARTITIONS"]
                )
                await self._notify_listeners("partitions", data["STATUS_PARTITIONS"])
            # Some panels may emit PARTITIONS instead of STATUS_PARTITIONS in realtime payloads.
            if "PARTITIONS" in data and "STATUS_PARTITIONS" not in data:
                self._logger.debug(
                    "Realtime partitions update (PARTITIONS): %s", data["PARTITIONS"]
                )
                await self._notify_listeners("partitions", data["PARTITIONS"])
            if "STATUS_ZONES" in data:
                self._logger.debug("Realtime zones update: %s", data["STATUS_ZONES"])
                await self._notify_listeners("zones", data["STATUS_ZONES"])
            if "STATUS_SYSTEM" in data:
                systems = data["STATUS_SYSTEM"]
                self._logger.debug(
                    "Updating state for systems: %s",
                    systems,
                )
                # Help diagnose panels that don't seem to update systems in UI/MQTT.
                if self._output_debug_verbose:
                    try:
                        if isinstance(systems, list):
                            sample = []
                            for it in systems[:6]:
                                if not isinstance(it, dict):
                                    continue
                                row = {"ID": it.get("ID")}
                                arm = it.get("ARM")
                                if isinstance(arm, dict):
                                    row["ARM"] = {"S": arm.get("S"), "D": arm.get("D")}
                                temp = it.get("TEMP")
                                if isinstance(temp, dict):
                                    row["TEMP"] = {"IN": temp.get("IN"), "OUT": temp.get("OUT")}
                                sample.append(row)
                            self._logger.info(
                                "WS1 REALTIME STATUS_SYSTEM n=%s sample=%s",
                                len(systems),
                                sample,
                            )
                        else:
                            self._logger.info(
                                "WS1 REALTIME STATUS_SYSTEM type=%s",
                                type(systems).__name__,
                            )
                    except Exception:
                        pass

                # Update the initial realtime SYSTEM data so getSystem() sees fresh values
                if self._realtimeInitialData is None:
                    self._realtimeInitialData = {}
                if "PAYLOAD" not in self._realtimeInitialData:
                    self._realtimeInitialData["PAYLOAD"] = {}
                self._realtimeInitialData["PAYLOAD"]["STATUS_SYSTEM"] = systems

                # Notify listeners (entities registered under 'systems')
                await self._notify_listeners("systems", systems)

            if "STATUS_CONNECTION" in data:
                conn = data["STATUS_CONNECTION"]
                self._logger.debug("Updating state for connection: %s", conn)

                if self._realtimeInitialData is None:
                    self._realtimeInitialData = {}
                if "PAYLOAD" not in self._realtimeInitialData:
                    self._realtimeInitialData["PAYLOAD"] = {}
                self._realtimeInitialData["PAYLOAD"]["STATUS_CONNECTION"] = conn

                await self._notify_listeners("connection", conn)

            if "STATUS_TEMPERATURES" in data:
                temps = data["STATUS_TEMPERATURES"]
                self._logger.debug("Updating state for temperatures: %s", temps)
                if self._realtimeInitialData is None:
                    self._realtimeInitialData = {}
                if "PAYLOAD" not in self._realtimeInitialData:
                    self._realtimeInitialData["PAYLOAD"] = {}
                self._realtimeInitialData["PAYLOAD"]["STATUS_TEMPERATURES"] = temps
                await self._notify_listeners("thermostats", temps)

            if "STATUS_HUMIDITY" in data:
                hum = data["STATUS_HUMIDITY"]
                self._logger.debug("Updating state for humidity: %s", hum)
                if self._realtimeInitialData is None:
                    self._realtimeInitialData = {}
                if "PAYLOAD" not in self._realtimeInitialData:
                    self._realtimeInitialData["PAYLOAD"] = {}
                self._realtimeInitialData["PAYLOAD"]["STATUS_HUMIDITY"] = hum
                await self._notify_listeners("thermostats", hum)

    """
    Registers a listener for a specific entity type.

    Args:
        entity_type (str): entity type (e.g. "lights")
        callback (function): function to call when an update is received
        for that entity type
    """

    def register_listener(self, entity_type, callback):
        if entity_type in self.listeners:
            self.listeners[entity_type].append(callback)

    """
    Processes the command queue in a loop.

    This function runs in a separate task and is responsible for sending
    commands to the Ksenia panel. It reads commands from the command queue,
    locks the websocket to ensure only one command is sent at a time,
    and sends the command using exeScenario or setOutput functions.

    If an exception occurs while sending a command, the error is logged
    and the command is not retried.
    """

    async def process_command_queue(self):
        self._logger.debug("Command queue started")
        while True:
            if not self._running:
                await asyncio.sleep(0.2)
                continue
            try:
                command_data = await asyncio.wait_for(self._command_queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            output_id, command = command_data["output_id"], command_data["command"]

            try:
                async with self._ws_lock:
                    if isinstance(command, dict) and command.get("type") == "PARTITION":
                        self._logger.debug(
                            "Setting partition %s MOD=%s", output_id, command.get("mod")
                        )
                        pin = command_data.get("pin") or self._pin
                        await armPartition(
                            self._ws,
                            self._loginId,
                            pin,
                            command_data,
                            self._pending_commands,
                            self._logger,
                        )
                    elif isinstance(command, dict) and command.get("type") == "BYPASS":
                        self._logger.debug(
                            "Setting zone %s BYP=%s", output_id, command.get("byp")
                        )
                        pin = command_data.get("pin") or self._pin
                        await bypZone(
                            self._ws,
                            self._loginId,
                            pin,
                            command_data,
                            self._pending_commands,
                            self._logger,
                        )
                    elif command == "SCENARIO":
                        self._logger.debug(f"Executing scenario {output_id}")
                        pin = command_data.get("pin") or self._pin
                        await exeScenario(
                            self._ws,
                            self._loginId,
                            pin,
                            command_data,
                            self._pending_commands,
                            self._logger,
                        )
                    elif isinstance(command, str):
                        self._logger.debug(f"Sending command {command} to {output_id}")
                        pin = command_data.get("pin") or self._pin
                        await setOutput(
                            self._ws,
                            self._loginId,
                            pin,
                            command_data,
                            self._pending_commands,
                            self._logger,
                        )
                    elif isinstance(command, int):
                        self._logger.debug(f"Sending dimmer command {command} to {output_id}")
                        pin = command_data.get("pin") or self._pin
                        await setOutput(
                            self._ws,
                            self._loginId,
                            pin,
                            command_data,
                            self._pending_commands,
                            self._logger,
                        )
                    elif isinstance(command, dict) and command.get("type") == "SCHEDULER":
                        patch = command.get("patch")
                        if not isinstance(patch, dict):
                            raise ValueError("SCHEDULER command requires patch dict")
                        resp = await writeCfg(
                            self._ws,
                            self._loginId,
                            self._logger,
                            {"CFG_SCHEDULER_TIMERS": [patch]},
                            dispatch_unhandled=self.handle_message,
                        )
                        ok = isinstance(resp, dict) and resp.get("RESULT") == "OK"
                        merged = None
                        if ok:
                            try:
                                if self._readData is None:
                                    self._readData = {}
                                timers = self._readData.get("CFG_SCHEDULER_TIMERS") or []
                                if not isinstance(timers, list):
                                    timers = []
                                merged = []
                                found = False
                                for t in timers:
                                    if isinstance(t, dict) and str(t.get("ID")) == str(patch.get("ID")):
                                        merged.append({**t, **patch})
                                        found = True
                                    else:
                                        merged.append(t)
                                if not found:
                                    merged.append(dict(patch))
                                self._readData["CFG_SCHEDULER_TIMERS"] = merged
                            except Exception:
                                pass
                            try:
                                if isinstance(merged, list):
                                    await self._notify_listeners("schedulers", merged)
                            except Exception:
                                pass
                        command_data["future"].set_result(bool(ok))
                    elif isinstance(command, dict) and command.get("type") == "THERMOSTAT":
                        patch = command.get("patch")
                        if not isinstance(patch, dict):
                            raise ValueError("THERMOSTAT command requires patch dict")
                        # NOTE: For CFG_THERMOSTATS, the app typically writes a compact object (not the full 24x7 schedule),
                        # while still including the essential fields. Sending the entire schedule can be huge and may be ignored.
                        # Build a minimal payload from the current config + patch (ID + ACT_* + TOF + season profile subset).
                        base_cfg = None
                        try:
                            if isinstance(self._readData, dict):
                                cfg_list = self._readData.get("CFG_THERMOSTATS") or []
                                if isinstance(cfg_list, list):
                                    for t in cfg_list:
                                        if isinstance(t, dict) and str(t.get("ID")) == str(patch.get("ID")):
                                            base_cfg = t
                                            break
                        except Exception:
                            base_cfg = None

                        def _pick_dict(src, key):
                            if isinstance(src, dict):
                                v = src.get(key)
                                if isinstance(v, dict):
                                    return v
                            return None

                        base_id = (base_cfg or {}).get("ID") if isinstance(base_cfg, dict) else None
                        tid = str(patch.get("ID") or base_id or "")
                        act_mode = str(patch.get("ACT_MODE") or (base_cfg or {}).get("ACT_MODE") or "OFF").upper()
                        act_sea = str(patch.get("ACT_SEA") or (base_cfg or {}).get("ACT_SEA") or "WIN").upper()
                        if act_sea not in ("WIN", "SUM"):
                            act_sea = "WIN"

                        out_cfg = {"ID": tid, "ACT_MODE": act_mode, "ACT_SEA": act_sea}

                        # Preserve MAN_HRS if available.
                        man_hrs = patch.get("MAN_HRS")
                        if man_hrs is None and isinstance(base_cfg, dict) and "MAN_HRS" in base_cfg:
                            man_hrs = base_cfg.get("MAN_HRS")
                        if man_hrs is not None:
                            out_cfg["MAN_HRS"] = man_hrs

                        # Preserve TOF parameters (hysteresis/options) if available.
                        tof = _pick_dict(patch, "TOF") or _pick_dict(base_cfg, "TOF")
                        if isinstance(tof, dict) and tof:
                            out_cfg["TOF"] = dict(tof)

                        # Merge season profile updates, but keep it compact.
                        for season_key in ("WIN", "SUM"):
                            season_patch = _pick_dict(patch, season_key)
                            if season_patch is None:
                                continue
                            base_season = _pick_dict(base_cfg, season_key) or {}
                            merged_season = {**base_season, **season_patch}
                            # Keep only thresholds + manual setpoint keys; drop 24h/day tables unless explicitly provided.
                            keep_keys = {"T1", "T2", "T3", "TM"}
                            compact = {k: v for k, v in merged_season.items() if k in keep_keys}
                            # If caller explicitly passed schedules (MON/TUE/...), keep only those.
                            schedule_keys = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "SD1", "SD2"}
                            for k in schedule_keys:
                                if k in season_patch:
                                    compact[k] = season_patch.get(k)
                            if compact:
                                out_cfg[season_key] = compact

                        # Carry any other small/unknown top-level keys (excluding huge season dicts).
                        for k, v in patch.items():
                            if k in ("ID", "ACT_MODE", "ACT_SEA", "MAN_HRS", "TOF", "WIN", "SUM"):
                                continue
                            out_cfg[k] = v

                        # Mobile app uses PAYLOAD_TYPE=CFG_THERMOSTATS; include PIN for compatibility.
                        pin = command_data.get("pin") or self._pin
                        resp = await writeCfgTyped(
                            self._ws,
                            self._loginId,
                            self._logger,
                            # The panel replies with PAYLOAD_TYPE=CFG_ALL; sending CFG_ALL here matches other writes
                            # (e.g. scheduler) and appears to be more broadly accepted across firmwares.
                            "CFG_ALL",
                            {"CFG_THERMOSTATS": [out_cfg]},
                            pin=pin,
                            dispatch_unhandled=self.handle_message,
                        )
                        ok = isinstance(resp, dict) and resp.get("RESULT") == "OK"
                        merged = None
                        if ok:
                            # After a successful write, re-read full thermostat config so the UI/state stays aligned
                            # with what the panel actually applied (and so we pick up any implicit fields it updates).
                            try:
                                therm_payload = await readThermostatsCfg(
                                    self._ws,
                                    self._loginId,
                                    self._logger,
                                    pin=pin,
                                    dispatch_unhandled=self.handle_message,
                                )
                                cfg_list = therm_payload.get("CFG_THERMOSTATS") if isinstance(therm_payload, dict) else None
                                if isinstance(cfg_list, list):
                                    if self._readData is None:
                                        self._readData = {}
                                    self._readData["CFG_THERMOSTATS"] = cfg_list
                                    await self._notify_listeners("thermostats_cfg", cfg_list)
                                    merged = cfg_list
                            except Exception:
                                pass
                            try:
                                if self._readData is None:
                                    self._readData = {}
                                cfg = self._readData.get("CFG_THERMOSTATS") or []
                                if not isinstance(cfg, list):
                                    cfg = []
                                merged = []
                                found = False
                                for t in cfg:
                                    if isinstance(t, dict) and str(t.get("ID")) == str(patch.get("ID")):
                                        # Merge patch into existing cfg, keeping nested season dict merges.
                                        updated = dict(t)
                                        for k, v in patch.items():
                                            if k in ("WIN", "SUM", "TOF") and isinstance(v, dict):
                                                cur = updated.get(k)
                                                if not isinstance(cur, dict):
                                                    cur = {}
                                                updated[k] = {**cur, **v}
                                            else:
                                                updated[k] = v
                                        merged.append(updated)
                                        found = True
                                    else:
                                        merged.append(t)
                                if not found:
                                    merged.append(dict(patch))
                                self._readData["CFG_THERMOSTATS"] = merged
                            except Exception:
                                pass
                            # Notify cfg listeners so UI updates immediately (no need to wait polling).
                            try:
                                if isinstance(merged, list):
                                    await self._notify_listeners("thermostats_cfg", merged)
                            except Exception:
                                pass
                        command_data["future"].set_result(bool(ok))
                    elif isinstance(command, dict) and command.get("type") == "CLEAR":
                        ptype = str(command.get("payload_type") or "")
                        pin = command_data.get("pin") or self._pin
                        resp = await clearCmd(
                            self._ws,
                            self._loginId,
                            pin,
                            self._logger,
                            ptype,
                            dispatch_unhandled=self.handle_message,
                        )
                        ok = isinstance(resp, dict) and resp.get("RESULT") == "OK"
                        command_data["future"].set_result(bool(ok))
            except Exception as e:
                self._logger.error(f"Error processing command {command} for {output_id}: {e}")
                try:
                    command_data["future"].set_result(False)
                except Exception:
                    pass

    """
    Sends a command for the specified output.

    Args:
        output_id (int): ID of the output
        command (str or int): command to send (as a string or as a number)

    Returns:
        bool: True if the command was sent successfully, False otherwise

    Examples:
        await self.send_command(1, "ON")
        await self.send_command(2, 50)
    """

    async def send_command(self, output_id, command, pin: str | None = None):
        future = asyncio.Future()
        if isinstance(command, (int, float)):
            try:
                command = str(int(command))
            except Exception:
                command = str(command)
        command_data = {
            "output_id": output_id,
            "command": command.upper() if isinstance(command, str) else command,
            "future": future,
            "command_id": 0,
        }
        if pin not in (None, ""):
            command_data["pin"] = str(pin)
        await self._command_queue.put(command_data)
        self._logger.debug(f"Command added to queue: {command} for {output_id}")

        try:
            success = await asyncio.wait_for(future, timeout=60)
            if not success:
                self._logger.warning(f"Command {command} for {output_id} timed out")
                return False
        except asyncio.TimeoutError:
            self._logger.warning(f"Timeout waiting for command {command} for {output_id}")
            return False
        return True

    async def updateScheduler(self, scheduler_id: int, patch: dict) -> bool:
        try:
            future = asyncio.Future()
            command_data = {
                "output_id": int(scheduler_id),
                "command": {"type": "SCHEDULER", "patch": {"ID": str(scheduler_id), **(patch or {})}},
                "future": future,
                "command_id": 0,
            }
            await self._command_queue.put(command_data)
            self._logger.debug("Scheduler update queued: %s %s", scheduler_id, patch)
            return bool(await asyncio.wait_for(future, timeout=20))
        except asyncio.TimeoutError:
            self._logger.warning("Timeout waiting scheduler update %s", scheduler_id)
            return False
        except Exception as exc:
            self._logger.error("updateScheduler failed: %s", exc)
            return False

    async def setAccountEnabled(self, account_id: int, dacc: str, pin: str | None = None) -> bool:
        """
        Enable/disable a user account via CFG_ACCOUNTS write.

        dacc: "F" (enabled) or "T" (disabled)
        """
        try:
            dacc = str(dacc or "").strip().upper()
            if dacc not in ("F", "T"):
                raise ValueError("DACC must be F/T")
            patch = {"ID": str(int(account_id)), "DACC": dacc}
            pin = pin or self._pin
            async with self._ws_lock:
                resp = await writeCfgTyped(
                    self._ws,
                    self._loginId,
                    self._logger,
                    "CFG_ALL",
                    {"CFG_ACCOUNTS": [patch]},
                    pin=pin,
                    dispatch_unhandled=self.handle_message,
                )
            ok = isinstance(resp, dict) and resp.get("RESULT") == "OK"
            if ok:
                try:
                    if self._readData is None:
                        self._readData = {}
                    cfg = self._readData.get("CFG_ACCOUNTS") or []
                    if not isinstance(cfg, list):
                        cfg = []
                    merged = []
                    found = False
                    for a in cfg:
                        if isinstance(a, dict) and str(a.get("ID")) == str(patch.get("ID")):
                            merged.append({**a, **patch})
                            found = True
                        else:
                            merged.append(a)
                    if not found:
                        merged.append(dict(patch))
                    self._readData["CFG_ACCOUNTS"] = merged
                except Exception:
                    pass
            return bool(ok)
        except Exception as exc:
            self._logger.error("setAccountEnabled failed: %s", exc)
            return False

    async def clearPanel(self, payload_type: str, pin: str | None = None) -> bool:
        try:
            future = asyncio.Future()
            command_data = {
                "output_id": 0,
                "command": {"type": "CLEAR", "payload_type": str(payload_type or "")},
                "future": future,
                "command_id": 0,
            }
            if pin not in (None, ""):
                command_data["pin"] = str(pin)
            await self._command_queue.put(command_data)
            return bool(await asyncio.wait_for(future, timeout=20))
        except asyncio.TimeoutError:
            self._logger.warning("Timeout waiting CLEAR %s", payload_type)
            return False
        except Exception as exc:
            self._logger.error("clearPanel failed: %s", exc)
            return False

    """
    Stops the WebSocketManager.

    This method sets the running flag to False and closes the WebSocket
    connection if it exists.
    """

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def force_reconnect(self):
        """
        Force-close the main websocket and start the reconnect loop.
        Safe to call even if already disconnected.
        """
        try:
            self._running = False
            await self._close_ws()
        except Exception:
            pass
        await self._reconnect_forever()

    """
    Turns on the specified output.

    Args:
        output_id (int): ID of the output to turn on
        brightness (int, optional): Brightness level (0-100). Defaults to None.

    Returns:
        bool: True if the output was turned on successfully, False otherwise
    """

    async def turnOnOutput(self, output_id, brightness=None):
        try:
            if brightness:
                success = await self.send_command(output_id, brightness)
            else:
                success = await self.send_command(output_id, "ON")
            if not success:
                self._logger.warning(f"Failed to turn on output {output_id}.")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error turning on output {output_id}: {e}")
            return False

    """
    Turns off the specified output.

    Args:
        output_id (int): ID of the output to turn off

    Returns:
        bool: True if the output was turned off successfully, False otherwise
    """

    async def turnOffOutput(self, output_id):
        try:
            output_id = int(output_id)
            cat = None
            try:
                outputs = (self._readData or {}).get("OUTPUTS", [])
                if isinstance(outputs, list):
                    for o in outputs:
                        if str(o.get("ID")) == str(output_id):
                            cat = str(o.get("CAT") or "").strip().upper()
                            break
            except Exception:
                cat = None

            # Some LIGHT outputs respond better to level=0 than OFF.
            success = await self.send_command(output_id, "OFF")
            if not success and cat == "LIGHT":
                self._logger.warning("OFF failed for LIGHT %s, retry with level 0", output_id)
                success = await self.send_command(output_id, "0")

            if not success:
                self._logger.warning(f"Failed to turn off output {output_id}.")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error turning off output {output_id}: {e}")
            return False

    """
    Raises the specified cover.

    Args:
        roll_id (int): ID of the cover to raise

    Returns:
        bool: True if the cover was raised successfully, False otherwise
    """

    async def raiseCover(self, roll_id):
        try:
            success = await self.send_command(roll_id, "UP")
            if not success:
                self._logger.warning(f"Failed to raise cover {roll_id}.")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error raising cover {roll_id}: {e}")
            return False

    """
    Lowers the specified cover.

    Args:
        roll_id (int): ID of the cover to lower

    Returns:
        bool: True if the cover was lowered successfully, False otherwise
    """

    async def lowerCover(self, roll_id):
        try:
            success = await self.send_command(roll_id, "DOWN")
            if not success:
                self._logger.warning(f"Failed to lower cover {roll_id}.")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error lowering cover {roll_id}: {e}")
            return False

    """
    Stops the specified cover.

    Args:
        roll_id (int): ID of the cover to stop

    Returns:
        bool: True if the cover was stopped successfully, False otherwise
    """

    async def stopCover(self, roll_id):
        try:
            success = await self.send_command(roll_id, "ALT")
            if not success:
                self._logger.warning(f"Failed to stop cover {roll_id}.")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error stopping cover {roll_id}: {e}")
            return False

    """
    Sets the position of the specified cover.

    Args:
        roll_id (int): ID of the cover to set the position
        position (int): Position to set (0-100)

    Returns:
        bool: True if the cover position was set successfully, False otherwise
    """

    async def setCoverPosition(self, roll_id, position):
        try:
            success = await self.send_command(roll_id, str(position))
            if not success:
                self._logger.warning(f"Failed to set cover position for {roll_id}.")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error setting cover position for {roll_id}: {e}")
            return False

    """
    Executes the specified scenario.

    Args:
        scenario_id (int): ID of the scenario to execute

    Returns:
        bool: True if the scenario was executed successfully, False otherwise
    """

    async def executeScenario(self, scenario_id, pin: str | None = None):
        try:
            success = await self.send_command(scenario_id, "SCENARIO", pin=pin)
            if not success:
                self._logger.error(f"Error executing scenario {scenario_id}")
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error executing scenario {scenario_id}: {e}")
            return False

    async def setPartitionMode(self, partition_id, mod: str, pin: str | None = None):
        try:
            mod_raw = str(mod or "").strip().upper()
            # Your panel reports IA/DA in realtime, but CMD_ARM_PARTITION typically accepts
            # legacy A/I/D as command MOD. Try those first, then fall back to IA/DA.
            if mod_raw in ("A", "DA", "ARM", "ARM_DELAY", "DELAY"):
                candidates = ["A", "DA"]
            elif mod_raw in ("I", "IA", "INSTANT", "ARM_INSTANT", "ARM_NOW"):
                candidates = ["I", "IA"]
            elif mod_raw in ("D", "DISARM"):
                candidates = ["D"]
            else:
                candidates = [mod_raw]

            for cand in candidates:
                cmd = {"type": "PARTITION", "mod": str(cand)}
                success = await self.send_command(int(partition_id), cmd, pin=pin)
                if success:
                    return True
                self._logger.warning("Failed to set partition %s MOD=%s.", partition_id, cand)
            return False
        except Exception as e:
            self._logger.error(f"Error setting partition {partition_id} MOD={mod}: {e}")
            return False

    async def armPartition(self, partition_id, pin: str | None = None):
        # Default "arm" uses the standard arming mode (with delays) like the C4 driver.
        return await self.setPartitionMode(partition_id, "A", pin=pin)

    async def armPartitionInstant(self, partition_id, pin: str | None = None):
        return await self.setPartitionMode(partition_id, "I", pin=pin)

    async def disarmPartition(self, partition_id, pin: str | None = None):
        return await self.setPartitionMode(partition_id, "D", pin=pin)

    async def setZoneBypass(self, zone_id, byp: str, pin: str | None = None):
        try:
            cmd = {"type": "BYPASS", "byp": str(byp).upper()}
            success = await self.send_command(int(zone_id), cmd, pin=pin)
            if not success:
                self._logger.warning("Failed to set zone %s BYP=%s.", zone_id, byp)
                return False
            return True
        except Exception as e:
            self._logger.error(f"Error setting zone {zone_id} BYP={byp}: {e}")
            return False

    async def bypassZoneOn(self, zone_id, pin: str | None = None):
        return await self.setZoneBypass(zone_id, "ON", pin=pin)

    async def bypassZoneOff(self, zone_id, pin: str | None = None):
        return await self.setZoneBypass(zone_id, "OFF", pin=pin)

    async def bypassZoneToggle(self, zone_id, pin: str | None = None):
        return await self.setZoneBypass(zone_id, "TGL", pin=pin)

    """
    Retrieves the list of lights combining static and real-time data.

    This method waits for the initial data to be available, then extracts the
    lights from the static read data and enriches them with real-time
    state information. If the initial data is not received, it logs an error
    and returns an empty list.

    :return: List of lights with their current states, each represented as a dictionary.
    :rtype: list
    """

    async def getLights(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._realtimeInitialData or not self._readData:
            self._logger.error("Initial data not received in getLights")
            return []
        lares_realtime = self._realtimeInitialData.get("PAYLOAD", {}).get("STATUS_OUTPUTS", [])
        lights = [
            output for output in self._readData.get("OUTPUTS", []) if output.get("CAT") == "LIGHT"
        ]
        lights_with_states = []
        for light in lights:
            light_id = light.get("ID")
            state_data = next(
                (state for state in lares_realtime if state.get("ID") == light_id), None
            )
            if state_data:
                state_data["STA"] = state_data.get("STA", "off").lower()
                state_data["POS"] = int(state_data.get("POS", 255))
                lights_with_states.append({**light, **state_data})
        return lights_with_states

    """
    Retrieves the list of roller blinds (covers) combining static and real-time data.

    This method waits for the initial data to be available, then extracts the
    roller blinds from the static read data and enriches them with real-time
    state information. If the initial data is not received, it logs an error
    and returns an empty list.

    :return: List of roller blinds with their current states, each represented as a dictionary.
    :rtype: list
    """

    async def getRolls(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._readData or not self._realtimeInitialData:
            self._logger.error("Initial data not received in getRolls")
            return []
        lares_realtime = self._realtimeInitialData.get("PAYLOAD", {}).get("STATUS_OUTPUTS", [])
        outputs = self._readData.get("OUTPUTS", [])
        rolls = [output for output in outputs if output.get("CAT") == "ROLL"]
        rolls_with_states = []
        for roll in rolls:
            roll_id = roll.get("ID")
            state_data = next(
                (state for state in lares_realtime if state.get("ID") == roll_id), None
            )
            if state_data:
                state_data["STA"] = state_data.get("STA", "off").lower()
                state_data["POS"] = int(state_data.get("POS", 255))
                rolls_with_states.append({**roll, **state_data})
        return rolls_with_states

    """
    Retrieves the list of switches combining static and real-time data.

    This method waits for the initial data to be available, then extracts the
    switches from the static read data and enriches them with real-time state information.
    If the initial data is not received, it logs an error and returns an empty list.

    :return: List of switches with their current states, each represented as a dictionary.
    :rtype: list
    """

    async def getSwitches(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._realtimeInitialData or not self._readData:
            self._logger.error("Initial data not received in getSwitches")
            return []
        lares_realtime = self._realtimeInitialData.get("PAYLOAD", {}).get("STATUS_OUTPUTS", [])
        switches = [output for output in self._readData.get("OUTPUTS", []) if output.get("CAT") != "LIGHT"]
        switches_with_states = []
        for switch in switches:
            switch_id = switch.get("ID")
            state_data = next(
                (state for state in lares_realtime if state.get("ID") == switch_id), None
            )
            if state_data:
                switches_with_states.append({**switch, **state_data})
        return switches_with_states

    """
    Retrieves the list of domus (domotic sensors) combining static and real-time data.

    This method waits for the initial data to be available, then extracts the
    domus from the static read data and enriches them with real-time state information.
    If the initial data is not received, it logs an error and returns an empty list.

    :return: List of domus with their current states, each represented as a dictionary.
    :rtype: list
    """

    async def getDom(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._readData or not self._realtimeInitialData:
            self._logger.error("Initial data not received in getDom")
            return []
        domus = [output for output in self._readData.get("BUS_HAS", []) if output.get("TYP") == "DOMUS"]
        lares_realtime = self._realtimeInitialData.get("PAYLOAD", {}).get(
            "STATUS_BUS_HA_SENSORS", []
        )
        domus_with_states = []
        for sensor in domus:
            sensor_id = sensor.get("ID")
            state_data = next(
                (state for state in lares_realtime if state.get("ID") == sensor_id), None
            )
            if state_data:
                domus_with_states.append({**sensor, **state_data})
        return domus_with_states

    """
    Retrieves the list of sensors of a specific type, combining static and real-time data.

    This method waits for the initial data to be available, then extracts the
    sensors of the given type from the static read data and enriches them with
    real-time state information. If the initial data is not received, it logs an
    error and returns an empty list.

    :param sName: The name of the sensor type to retrieve.
    :type sName: str
    :return: List of sensors with their current states, each represented as a dictionary.
    :rtype: list
    """

    async def getSensor(self, sName):
        await self.wait_for_initial_data(timeout=5)
        if not self._readData or not self._realtimeInitialData:
            self._logger.error("Initial data not received in getSensor")
            return []
        sensorList = self._readData.get(sName, [])
        lares_realtime = self._realtimeInitialData.get("PAYLOAD", {}).get(
            "STATUS_" + sName, []
        )
        sensor_with_states = []
        for sensor in sensorList:
            sensor_id = sensor.get("ID")
            state_data = next(
                (state for state in lares_realtime if state.get("ID") == sensor_id), None
            )
            if state_data:
                sensor_with_states.append({**sensor, **state_data})
        return sensor_with_states

    """
    Retrieves the list of scenarios available in the system.

    This function waits for initial data to be available and then extracts
    the scenarios from the read data. If the initial data is not received,
    it logs an error and returns an empty list.

    :return: List of scenarios, each represented as a dictionary with keys as
             scenario attributes.
    """

    async def getScenarios(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._readData:
            self._logger.error("Initial data not received in getScenarios")
            return []
        scenarios = self._readData.get("SCENARIOS", [])
        return scenarios

    async def getSchedulers(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._readData:
            self._logger.error("Initial data not received in getSchedulers")
            return []
        timers = self._readData.get("CFG_SCHEDULER_TIMERS", []) or []
        if not isinstance(timers, list):
            return []
        scenarios = self._readData.get("SCENARIOS", []) or []
        sce_map = {}
        for s in scenarios:
            if isinstance(s, dict) and s.get("ID") is not None:
                sce_map[str(s.get("ID"))] = s.get("DES") or s.get("NM") or s.get("ID")
        enriched = []
        for t in timers:
            if not isinstance(t, dict):
                continue
            sce_id = str(t.get("SCE") or "")
            if sce_id and sce_id in sce_map:
                enriched.append({**t, "SCE_NAME": sce_map.get(sce_id)})
            else:
                enriched.append(dict(t))
        return enriched

    async def getThermostats(self):
        await self.wait_for_initial_data(timeout=5)
        if not self._readData and not self._realtimeInitialData:
            self._logger.error("Initial data not received in getThermostats")
            return []

        cfg_list = (self._readData or {}).get("CFG_THERMOSTATS") or []
        if not isinstance(cfg_list, list):
            cfg_list = []

        # Friendly names come from TEMPERATURES/HUMIDITY lists (as seen in READ_RES),
        # where the thermostat ID is usually referenced by ID_TH.
        name_by_id = {}
        for entry in ((self._readData or {}).get("TEMPERATURES") or []):
            if not isinstance(entry, dict):
                continue
            des = entry.get("DES")
            th_id = entry.get("ID_TH")
            if th_id is None:
                th_id = entry.get("ID")
            if th_id is not None and isinstance(des, str) and des.strip():
                name_by_id[str(th_id)] = des.strip()
        for entry in ((self._readData or {}).get("HUMIDITY") or []):
            if not isinstance(entry, dict):
                continue
            des = entry.get("DES")
            th_id = entry.get("ID_TH")
            if th_id is None:
                th_id = entry.get("ID")
            if th_id is not None and isinstance(des, str) and des.strip():
                name_by_id.setdefault(str(th_id), des.strip())

        payload = (self._realtimeInitialData or {}).get("PAYLOAD", {}) if self._realtimeInitialData else {}
        temps = payload.get("STATUS_TEMPERATURES") or []
        hums = payload.get("STATUS_HUMIDITY") or []
        if not isinstance(temps, list):
            temps = []
        if not isinstance(hums, list):
            hums = []

        temp_by_id = {str(x.get("ID")): x for x in temps if isinstance(x, dict) and x.get("ID") is not None}
        hum_by_id = {str(x.get("ID")): x for x in hums if isinstance(x, dict) and x.get("ID") is not None}

        ids = set()
        for x in cfg_list:
            if isinstance(x, dict) and x.get("ID") is not None:
                ids.add(str(x.get("ID")))
        ids.update(temp_by_id.keys())
        ids.update(hum_by_id.keys())

        out = []
        for tid in sorted(ids, key=lambda s: int(s) if str(s).isdigit() else str(s)):
            cfg = next((x for x in cfg_list if isinstance(x, dict) and str(x.get("ID")) == tid), {}) or {}
            rt = temp_by_id.get(tid) or {}
            hum = hum_by_id.get(tid) or {}
            merged = {"ID": cfg.get("ID") or rt.get("ID") or hum.get("ID") or tid}
            if isinstance(cfg, dict):
                merged.update(cfg)
            if isinstance(rt, dict):
                merged.update(rt)
            if isinstance(hum, dict):
                merged.update(hum)
            if "DES" not in merged and tid in name_by_id:
                merged["DES"] = name_by_id.get(tid)
            out.append(merged)
        return out

    async def updateThermostat(self, thermostat_id: int, patch: dict):
        if not isinstance(patch, dict):
            return False
        if patch.get("ID") is None:
            patch = {**patch, "ID": str(thermostat_id)}
        future = asyncio.get_running_loop().create_future()
        await self._command_queue.put(
            {"output_id": thermostat_id, "command": {"type": "THERMOSTAT", "patch": patch}, "future": future}
        )
        return await future

    """
    Retrieves the list of systems (plants) combining static and real-time data.

    :return: List of systems, each with the following keys:
        - ID: System ID
        - ARM: Armed status (True/False)
    """

    async def getSystem(self):
        await self.wait_for_initial_data(timeout=5)

        sys_list = []

        # Prefer realtime STATUS_SYSTEM if available
        if self._realtimeInitialData:
            payload = self._realtimeInitialData.get("PAYLOAD", {})
            sys_list = payload.get("STATUS_SYSTEM", []) or []

        # Fallback: use static snapshot from _readData
        if (not sys_list) and self._readData:
            sys_list = self._readData.get("STATUS_SYSTEM", []) or []

        if not sys_list:
            self._logger.error("No system data available in getSystem")
            return []

        systems = []
        for sys in sys_list:
            systems.append(
                {
                    "ID": sys.get("ID"),
                    "ARM": sys.get("ARM", {}),
                }
            )
        return systems
