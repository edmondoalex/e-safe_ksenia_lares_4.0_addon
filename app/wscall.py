"""
Huge thanks to @realnot16 for these functions!
"""

import asyncio
import inspect
import websockets
import time, json
from crc import addCRC

# Types of ksenia information required

read_types ='["OUTPUTS","BUS_HAS","SCENARIOS","POWER_LINES","PARTITIONS","ZONES","STATUS_SYSTEM","CFG_SCHEDULER_TIMERS","CFG_HOLIDAYS","TEMPERATURES","HUMIDITY","CFG_THERMOSTATS","CFG_ACCOUNTS"]'
realtime_types='["STATUS_OUTPUTS","STATUS_BUS_HA_SENSORS","STATUS_POWER_LINES","STATUS_PARTITIONS","STATUS_ZONES","STATUS_SYSTEM","STATUS_CONNECTION","STATUS_TEMPERATURES","STATUS_HUMIDITY"]'

cmd_id = 1


async def _dispatch_unhandled(dispatch_unhandled, message: dict):
    if dispatch_unhandled is None:
        return
    try:
        res = dispatch_unhandled(message)
        if inspect.isawaitable(res):
            await res
    except Exception:
        # Never let auxiliary dispatch break the main request/response loop.
        return

"""
Send a login command to the websocket.

:param websocket: Websocket client object
:param pin: Pin to login with
:param _LOGGER: Logger object
:return: ID of the login session
:rtype: int
"""
async def ws_login(websocket, pin, _LOGGER):
    global cmd_id
    json_cmd = addCRC('{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"LOGIN", "ID": "1", "PAYLOAD_TYPE":"USER", "PAYLOAD":{"PIN":"' + pin + '"}, "TIMESTAMP":"' + str(int(time.time())) + '", "CRC_16":"0x0000"}')
    
    try:
        await websocket.send(json_cmd)
        json_resp = await websocket.recv()
    except Exception as e:
        _LOGGER.error(f"Error during connection: {e}")
    response = json.loads(json_resp)
    login_id = -1
    if(response["PAYLOAD"]["RESULT"] == "OK"):
        login_id = int(response["PAYLOAD"]["ID_LOGIN"])
    return login_id


"""
Start real-time monitoring for the given login_id.

:param websocket: Websocket client object
:param login_id: ID of the login session
:param _LOGGER: Logger object
:return: Initial data from the real-time API
:rtype: dict
"""
async def realtime(websocket,login_id,_LOGGER):
    _LOGGER.info("sending realtime first message")
    global cmd_id
    cmd_id += 1
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"REALTIME", "ID":"'
        + str(cmd_id)
        + '", "PAYLOAD_TYPE":"REGISTER", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","TYPES":'+str(realtime_types)+'},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
        )
    try:
        await websocket.send(json_cmd)
        _LOGGER.info("started realtime monitoring - returning intial data")
        json_resp_states = await websocket.recv()
        response_realtime = json.loads(json_resp_states)
    except Exception as e:
        _LOGGER.error(f"Realtime call failed: {e}")
        _LOGGER.error(response_realtime)
    return response_realtime


"""
Refresh realtime snapshot for a subset of TYPES.

This sends another REALTIME/REGISTER message and waits for a reply containing at least
one of the requested payload keys.
"""
async def realtime_select(websocket, login_id, _LOGGER, types, dispatch_unhandled=None):
    global cmd_id
    cmd_id += 1
    types_list = types if isinstance(types, list) else []
    types_json = json.dumps(types_list)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"REALTIME", "ID":"'
        + str(cmd_id)
        + '", "PAYLOAD_TYPE":"REGISTER", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","TYPES":'
        + types_json
        + '},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            payload = resp.get("PAYLOAD") if isinstance(resp, dict) else None
            if isinstance(payload, dict):
                for t in types_list:
                    if t in payload:
                        return resp
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"realtime_select call failed: {e}")
        return {}

"""
Fetch panel system version/info (MODEL/FW/WS/etc).

This is a synchronous request/response and should be used before the async listener
starts consuming websocket messages.
"""
async def systemVersion(websocket, login_id, _LOGGER, dispatch_unhandled=None):
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"SYSTEM_VERSION", "ID":"'
        + expected_id
        + '", "PAYLOAD_TYPE":"REQUEST", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '"},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        # The panel can send other messages; wait for the matching reply.
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "SYSTEM_VERSION_RES":
                # Some panels don't echo back the same ID reliably; accept any SYSTEM_VERSION_RES.
                if str(resp.get("ID")) != expected_id:
                    _LOGGER.warning(
                        "SYSTEM_VERSION_RES id mismatch (expected %s, got %s) - accepting reply",
                        expected_id,
                        resp.get("ID"),
                    )
                return resp.get("PAYLOAD") or {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"SYSTEM_VERSION call failed: {e}")
        return {}


"""
Extract the initial data from the Ksenia panel.

:param websocket: Websocket client object
:param login_id: ID of the login session
:param _LOGGER: Logger object
:return: Initial data from the Ksenia panel
:rtype: dict
"""
async def readData(websocket,login_id,_LOGGER):
    _LOGGER.info("extracting data")
    global cmd_id
    cmd_id += 1
    json_cmd = addCRC(
            '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"READ","ID":"'
            + str(cmd_id)
            + '","PAYLOAD_TYPE":"MULTI_TYPES","PAYLOAD":{"ID_LOGIN":"'
            + str(login_id)
            + '","ID_READ":"1","TYPES":'+str(read_types)+'},"TIMESTAMP":"'
            + str(int(time.time()))
            + '","CRC_16":"0x0000"}'
        )
    try:
        await websocket.send(json_cmd)
        json_resp_states = await websocket.recv()
        response_read = json.loads(json_resp_states)
    except Exception as e:
        _LOGGER.error(f"readData call failed: {e}")
        _LOGGER.error(response_read)
    return response_read["PAYLOAD"]


"""
Read scheduler timers/holidays configuration (Programmatori Orari).
"""
async def readSchedulers(websocket, login_id, _LOGGER, dispatch_unhandled=None):
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"READ","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"MULTI_TYPES","PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","ID_READ":"1","TYPES":["CFG_SCHEDULER_TIMERS","CFG_HOLIDAYS"]},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "READ_RES" and str(resp.get("ID")) == expected_id:
                payload = resp.get("PAYLOAD") or {}
                return payload if isinstance(payload, dict) else {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"readSchedulers call failed: {e}")
        return {}

"""
Read zones snapshot (ZONES) via READ/MULTI_TYPES.

Some panels don't push all fields (e.g. BYP) via realtime STATUS_ZONES, so we
poll ZONES periodically and diff in the manager.
"""
async def readZones(websocket, login_id, _LOGGER, dispatch_unhandled=None):
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"READ","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"MULTI_TYPES","PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","ID_READ":"1","TYPES":["ZONES"]},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "READ_RES" and str(resp.get("ID")) == expected_id:
                payload = resp.get("PAYLOAD") or {}
                return payload if isinstance(payload, dict) else {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"readZones call failed: {e}")
        return {}

"""
Read full thermostat configuration (including schedules) as the mobile app does:
CMD: READ
PAYLOAD_TYPE: CFG_THERMOSTATS
PAYLOAD: { ID_LOGIN, ID_READ:"1" }
Response:
CMD: READ_RES
PAYLOAD_TYPE: CFG_THERMOSTATS
PAYLOAD: { RESULT, CFG_THERMOSTATS:[...], PRG_CHECK }
"""
async def readThermostatsCfg(websocket, login_id, _LOGGER, pin: str | None = None, dispatch_unhandled=None):
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    # Try the most "app-like" shape first.
    payload = {"ID_LOGIN": str(login_id), "ID_READ": "1"}
    if pin not in (None, ""):
        payload["PIN"] = str(pin)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"READ","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"CFG_THERMOSTATS","PAYLOAD":'
        + json.dumps(payload, ensure_ascii=False)
        + ',"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 15
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "READ_RES" and str(resp.get("PAYLOAD_TYPE") or "").upper() == "CFG_THERMOSTATS":
                if str(resp.get("ID")) != expected_id:
                    _LOGGER.warning(
                        "readThermostatsCfg: READ_RES id mismatch (expected %s, got %s) - accepting reply",
                        expected_id,
                        resp.get("ID"),
                    )
                payload = resp.get("PAYLOAD") or {}
                return payload if isinstance(payload, dict) else {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error("readThermostatsCfg failed: %r (%s)", e, type(e).__name__)
        return {}


"""
Send a command to set the status of an output.

:param websocket: Websocket client object
:param login_id: ID of the login session
:param pin: PIN to use for login
:param command_data: Dictionary with the command data
:param queue: Queue to store the command data
:param logger: Logger object
"""
async def setOutput(websocket, login_id, pin, command_data, queue, logger):
    global cmd_id
    cmd_id += 1
    command_id_str = str(cmd_id)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"CMD_USR", "ID": "'
        + command_id_str +
        '", "PAYLOAD_TYPE":"CMD_SET_OUTPUT", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id) +
        '","PIN":"' + pin +
        '","OUTPUT":{"ID":"' + str(command_data["output_id"]) +
        '","STA":"' + str(command_data["command"]) +
        '"}},"TIMESTAMP":"' + str(int(time.time())) +
        '", "CRC_16":"0x0000"}'
    )
    try:
        command_data["command_id"] = command_id_str
        queue[command_id_str] = command_data
        await websocket.send(json_cmd)
        logger.debug(f"setOutput: Sent command {command_id_str} for output {command_data['output_id']} with command {command_data['command']}")
        asyncio.create_task(wait_for_future(command_data["future"], command_id_str, queue, logger))
    except Exception as e:
        logger.error(f"WSCALL - setOutput call failed: {e}")
        queue.pop(command_id_str, None)


"""
Execute the relative scenario

Parameters:
websocket (WebSocketServerProtocol): the websocket to send the command to
login_id (int): the login id of the user
pin (str): the pin of the user
command_data (dict): the data of the command
queue (dict): the queue of the commands
logger (Logger): the logger of the component
"""
async def exeScenario(websocket, login_id, pin, command_data, queue, logger):
    logger.info("WSCALL - trying executing scenario")
    global cmd_id
    cmd_id = cmd_id + 1
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"CMD_USR", "ID": "'
        + str(cmd_id)
        + '", "PAYLOAD_TYPE":"CMD_EXE_SCENARIO", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","PIN":"'
        + pin
        + '","SCENARIO":{"ID":"'
        + str(command_data["output_id"])
        + '"}}, "TIMESTAMP":"'
        + str(int(time.time()))
        + '", "CRC_16":"0x0000"}'
    )
    try:
        command_data["command_id"]=cmd_id
        queue[str(cmd_id)]= command_data
        await websocket.send(json_cmd)

        #delete item if future not satisfied
        asyncio.create_task(wait_for_future(command_data["future"], cmd_id, queue, logger))

    except Exception as e:
        logger.error(f"WSCALL -  executeScenario call failed: {e}")
        queue.pop(str(cmd_id), None)

"""
Set the arming mode of a partition (arm/disarm/etc).

This uses the Ksenia CMD_ARM_PARTITION payload type, as in the Control4 driver.

command_data fields used:
  - output_id: partition ID
  - command: dict like {"type":"PARTITION","mod":"I"} (I=arm, D=disarm)
"""
async def armPartition(websocket, login_id, pin, command_data, queue, logger):
    global cmd_id
    cmd_id += 1
    command_id_str = str(cmd_id)

    partition_id = str(command_data.get("output_id"))
    cmd = command_data.get("command") or {}
    mod = str((cmd.get("mod") if isinstance(cmd, dict) else "") or "")
    if not mod:
        raise ValueError("armPartition requires command {'type':'PARTITION','mod':...}")

    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"CMD_USR", "ID": "'
        + command_id_str
        + '", "PAYLOAD_TYPE":"CMD_ARM_PARTITION", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","PIN":"'
        + pin
        + '","PARTITION":{"ID":"'
        + partition_id
        + '","MOD":"'
        + mod
        + '"}},"TIMESTAMP":"'
        + str(int(time.time()))
        + '", "CRC_16":"0x0000"}'
    )

    try:
        command_data["command_id"] = command_id_str
        queue[command_id_str] = command_data
        await websocket.send(json_cmd)
        logger.debug(
            f"armPartition: Sent command {command_id_str} for partition {partition_id} MOD={mod}"
        )
        asyncio.create_task(
            wait_for_future(command_data["future"], command_id_str, queue, logger)
        )
    except Exception as e:
        logger.error(f"WSCALL - armPartition call failed: {e}")
        queue.pop(command_id_str, None)

"""
Set bypass status for a zone (ON/OFF/TGL).

command_data fields used:
  - output_id: zone ID
  - command: dict like {"type":"BYPASS","byp":"ON"}
"""
async def bypZone(websocket, login_id, pin, command_data, queue, logger):
    global cmd_id
    cmd_id += 1
    command_id_str = str(cmd_id)

    zone_id = str(command_data.get("output_id"))
    cmd = command_data.get("command") or {}
    byp_raw = str((cmd.get("byp") if isinstance(cmd, dict) else "") or "").upper()
    # Newer Ksenia web UI uses AUTO/NO for bypass, not ON/OFF.
    byp_map = {"ON": "AUTO", "OFF": "NO", "1": "AUTO", "0": "NO"}
    byp = byp_map.get(byp_raw, byp_raw)
    if byp not in ("AUTO", "NO", "TGL", "ON", "OFF"):
        raise ValueError("bypZone requires command {'type':'BYPASS','byp':'AUTO|NO|TGL'}")

    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant", "RECEIVER":"", "CMD":"CMD_USR", "ID": "'
        + command_id_str
        + '", "PAYLOAD_TYPE":"CMD_BYP_ZONE", "PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","PIN":"'
        + pin
        + '","ZONE":{"ID":"'
        + zone_id
        + '","BYP":"'
        + byp
        + '"}},"TIMESTAMP":"'
        + str(int(time.time()))
        + '", "CRC_16":"0x0000"}'
    )

    try:
        command_data["command_id"] = command_id_str
        queue[command_id_str] = command_data
        await websocket.send(json_cmd)
        logger.debug(f"bypZone: Sent command {command_id_str} for zone {zone_id} BYP={byp}")
        asyncio.create_task(
            wait_for_future(command_data["future"], command_id_str, queue, logger)
        )
    except Exception as e:
        logger.error(f"WSCALL - bypZone call failed: {e}")
        queue.pop(command_id_str, None)


"""
Fetch event logs (Registro Eventi) - last N logs.

Example (from web UI):
CMD: LOGS
PAYLOAD_TYPE: GET_LAST_LOGS
PAYLOAD: { ID_LOGIN, ID_LOG:"MAIN", ITEMS_LOG:"150", ITEMS_TYPE:["ALL"] }
"""
async def getLogs(websocket, login_id, _LOGGER, id_log="MAIN", items=150, items_type=None, dispatch_unhandled=None):
    if items_type is None:
        items_type = ["ALL"]
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"LOGS","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"GET_LAST_LOGS","PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","ID_LOG":"'
        + str(id_log)
        + '","ITEMS_LOG":"'
        + str(items)
        + '","ITEMS_TYPE":'
        + json.dumps(items_type)
        + '},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "LOGS_RES":
                if str(resp.get("ID")) != expected_id:
                    _LOGGER.warning(
                        "LOGS_RES id mismatch (expected %s, got %s) - accepting reply",
                        expected_id,
                        resp.get("ID"),
                    )
                return resp.get("PAYLOAD") or {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"getLogs call failed: {e}")
        return {}


"""
Write config updates (e.g. CFG_SCHEDULER_TIMERS) using WRITE_CFG / WRITE_CFG_RES.

Example (from web UI):
CMD: WRITE_CFG
PAYLOAD_TYPE: CFG_ALL
PAYLOAD: { ID_LOGIN, CFG_SCHEDULER_TIMERS:[{ID:"1",EN:"T"}] }
"""
async def writeCfg(websocket, login_id, _LOGGER, payload_patch: dict, dispatch_unhandled=None):
    if not isinstance(payload_patch, dict):
        return {}
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"WRITE_CFG","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"CFG_ALL","PAYLOAD":'
        + json.dumps({"ID_LOGIN": str(login_id), **payload_patch}, ensure_ascii=False)
        + ',"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "WRITE_CFG_RES":
                if str(resp.get("ID")) != expected_id:
                    _LOGGER.warning(
                        "WRITE_CFG_RES id mismatch (expected %s, got %s) - accepting reply",
                        expected_id,
                        resp.get("ID"),
                    )
                return resp.get("PAYLOAD") or {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"writeCfg call failed: {e}")
        return {}

"""
Write config updates with a custom PAYLOAD_TYPE and optional PIN (some sections may require it).

The mobile app uses e.g.:
CMD: WRITE_CFG
PAYLOAD_TYPE: CFG_THERMOSTATS
PAYLOAD: { ID_LOGIN, PIN?, CFG_THERMOSTATS:[{...}] }
"""
async def writeCfgTyped(websocket, login_id, _LOGGER, payload_type: str, payload_patch: dict, pin: str | None = None, dispatch_unhandled=None):
    if not isinstance(payload_patch, dict):
        return {}
    ptype = str(payload_type or "").strip().upper()
    if not ptype:
        ptype = "CFG_ALL"
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    payload = {"ID_LOGIN": str(login_id), **payload_patch}
    if pin not in (None, ""):
        payload["PIN"] = str(pin)
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"WRITE_CFG","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"'
        + ptype
        + '","PAYLOAD":'
        + json.dumps(payload, ensure_ascii=False)
        + ',"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        payload_preview = json.dumps(payload_patch, ensure_ascii=False)
        _LOGGER.info("WRITE_CFG(%s) bytes=%s -> %s", ptype, len(payload_preview.encode("utf-8")), payload_preview)
        await websocket.send(json_cmd)
        deadline = time.time() + 20
        seen = 0
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if seen < 5:
                try:
                    _LOGGER.info(
                        "WRITE_CFG wait: got CMD=%s PAYLOAD_TYPE=%s ID=%s",
                        resp.get("CMD"),
                        resp.get("PAYLOAD_TYPE"),
                        resp.get("ID"),
                    )
                except Exception:
                    pass
                seen += 1
            if resp.get("CMD") == "WRITE_CFG_RES":
                if str(resp.get("ID")) != expected_id:
                    _LOGGER.warning(
                        "WRITE_CFG_RES id mismatch (expected %s, got %s) - accepting reply",
                        expected_id,
                        resp.get("ID"),
                    )
                return resp.get("PAYLOAD") or {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error("writeCfgTyped call failed: %r (%s)", e, type(e).__name__)
        return {}


"""
Clear command (reset) as used by web UI:
CMD: CLEAR
PAYLOAD_TYPE: CYCLES_OR_MEMORIES | COMMUNICATIONS | FAULTS_MEMORY
PAYLOAD: { ID_LOGIN, PIN }
"""
async def clearCmd(websocket, login_id, pin, _LOGGER, payload_type: str, dispatch_unhandled=None):
    global cmd_id
    cmd_id += 1
    expected_id = str(cmd_id)
    ptype = str(payload_type or "").strip().upper()
    if not ptype:
        return {}
    json_cmd = addCRC(
        '{"SENDER":"HomeAssistant","RECEIVER":"","CMD":"CLEAR","ID":"'
        + expected_id
        + '","PAYLOAD_TYPE":"'
        + ptype
        + '","PAYLOAD":{"ID_LOGIN":"'
        + str(login_id)
        + '","PIN":"'
        + str(pin)
        + '"},"TIMESTAMP":"'
        + str(int(time.time()))
        + '","CRC_16":"0x0000"}'
    )
    try:
        await websocket.send(json_cmd)
        deadline = time.time() + 10
        while True:
            timeout = max(0.1, deadline - time.time())
            if timeout <= 0:
                return {}
            json_resp = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            resp = json.loads(json_resp)
            if resp.get("CMD") == "CLEAR_RES":
                if str(resp.get("ID")) != expected_id:
                    _LOGGER.warning(
                        "CLEAR_RES id mismatch (expected %s, got %s) - accepting reply",
                        expected_id,
                        resp.get("ID"),
                    )
                return resp.get("PAYLOAD") or {}
            await _dispatch_unhandled(dispatch_unhandled, resp)
    except Exception as e:
        _LOGGER.error(f"clearCmd call failed: {e}")
        return {}


"""
Waits for the specified future to complete within a timeout period.

Args:
    future (asyncio.Future): The future object to wait for.
    cmd_id (int): The command identifier associated with the future.
    queue (dict): A dictionary representing the queue of pending commands.
    logger (logging.Logger): Logger instance for logging messages.

Logs an error and removes the command from the queue if the future times out
or an exception occurs during the waiting period.
"""
async def wait_for_future(future, command_id, queue, logger, timeout=60):
    try:
        await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        logger.error(f"Command {command_id} timed out - deleting from pending queue")
        queue.pop(command_id, None)
    except Exception as e:
        logger.error(f"Error in wait_for_future for command {command_id}: {e}")
