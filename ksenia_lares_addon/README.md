# e-Safe Ksenia Lares 4.0  inerface ed mqtt device con E-Manager

Add-on E-Manager  che collega una centrale **Ksenia Lares** e pubblica in **MQTT** (con **MQTT Discovery**) sensori, partizioni, scenari, uscite, termostati, programmatori e utenti, oltre a fornire due UI web (Security + index_debug).

## Quick start (5 minuti)
1. Configura le opzioni minime dell’add-on:
   - `ksenia_host`, `ksenia_port`, `ksenia_pin`
   - `mqtt_host` (di solito `core-mosquitto`)
   - `mqtt_prefix` (consigliato `e-safe`)
2. Avvia l’add-on.
3. In E-Manager : verifica in **Impostazioni → Dispositivi e servizi → MQTT** che compaiano i device `e-safe ...`.
4. Se non compaiono o sono incoerenti: usa `cleanup_discovery` → `republish_discovery` (da `index_debug` o endpoint add-on).
5. Apri la UI dall’add-on (Ingress) e scegli `Security UI` o `index_debug`.

## Prerequisiti
- E-Manager  Supervisor (Add-on).
- Broker MQTT funzionante (es. Mosquitto add-on).
- Centrale Ksenia raggiungibile via rete (IP/porta) e PIN.

## Installazione
1. Copia la cartella dell’add-on in `addons/local/ksenia_lares_addon` (o installa dal tuo repository locale).
2. Installa l’add-on da **Impostazioni → Componenti aggiuntivi → Add-on Store → Local add-ons**.
3. Configura le opzioni (vedi sotto).
4. Avvia l’add-on.

## Configurazione (opzioni)
Le principali opzioni sono in **Configurazione** dell’add-on:

- `ksenia_host` (obbligatorio): IP/hostname della centrale (es. `192.168.2.10`)
- `ksenia_port` (obbligatorio): porta HTTP/WS della centrale (tipicamente `80`)
- `ksenia_pin` (obbligatorio): PIN per comandi

- `mqtt_host`: host broker MQTT (default `core-mosquitto`)
- `mqtt_port`: porta broker (default `1883`)
- `mqtt_user` / `mqtt_password`: credenziali broker (se usate)
- `mqtt_prefix`: prefisso topic applicativo (default `ksenia`, tipicamente `e-safe`)
- `mqtt_debug_verbose`: log MQTT più verboso

UI / sicurezza:
- `web_pin_session_required`: richiede sessione PIN per alcune azioni UI
- `web_pin_session_minutes_default`: durata sessione (minuti)
- `security_cmd_ws_idle_timeout_sec`: timeout WS comandi

Porte:
- `debug_ui_port`: porta container per UI debug (default `8080`)
- `security_ui_port`: porta container per Security UI (default `8081`)

Icon HTTP (opzionale):
- `icon_http_enabled`: abilita notifiche HTTP verso un endpoint esterno
- `icon_http_base_url`, `icon_http_token`, `icon_http_timeout`

## Come funziona (in breve)
L’add-on:
1. Si connette alla centrale e legge una “snapshot” dello stato.
2. Mantiene un canale realtime (WS) per aggiornamenti.
3. Pubblica gli stati su MQTT sotto `mqtt_prefix/...` (es. `e-safe/zones/74`).
4. Pubblica la configurazione MQTT Discovery su `homeassistant/.../config` (retain=true) così E-Manager  crea automaticamente le entità.
5. Espone due UI web:
   - **Security UI**: controllo allarme/partizioni + viste operative.
   - **index_debug**: strumenti, snapshot, diagnostica.

## MQTT topics principali
Stati (esempi):
- Zone: `e-safe/zones/<id>`
- Partizioni: `e-safe/partitions/<id>`
- Uscite: `e-safe/outputs/<id>`
- Scenari: `e-safe/scenarios/<id>`
- Termostati: `e-safe/thermostats/<id>/...`
- Programmatori: `e-safe/schedulers/<id>`
- Utenti: `e-safe/accounts/<id>`

Comandi (esempi):
- Uscite: `e-safe/cmd/output/<id>` payload `ON` / `OFF`
- Scenari: `e-safe/cmd/scenario/<id>` payload `EXECUTE`
- Partizioni: `e-safe/cmd/partition/<id>` payload `DISARM` / `ARM_AWAY` / `ARM_HOME` / `ARM_NIGHT`
- Bypass zona: `e-safe/cmd/zone_bypass/<id>` payload `AUTO` / `NO`
- Termostato:
  - setpoint: `e-safe/cmd/thermostat/<id>/temperature`
  - mode: `e-safe/cmd/thermostat/<id>/mode`
  - preset: `e-safe/cmd/thermostat/<id>/preset_mode`
- Programmatori: `e-safe/cmd/scheduler/<id>` payload `ON` / `OFF`
- Utenti: `e-safe/cmd/account/<id>` payload `ON` / `OFF`

## Esempi MQTT (copia-incolla)
Sostituisci l’host MQTT e i topic secondo la tua configurazione (`mqtt_prefix`).

Esempi con Mosquitto add-on (E-Manager  OS/Supervised):
- Pubblica su uscita 36 (ON/OFF):
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/output/36" -m "ON"`
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/output/36" -m "OFF"`
- Esegui scenario 1:
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/scenario/1" -m "EXECUTE"`
- Partizione 1 (disinserisci / inserisci totale):
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/partition/1" -m "DISARM"`
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/partition/1" -m "ARM_AWAY"`
- Bypass zona 74 (AUTO / NO):
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/zone_bypass/74" -m "AUTO"`
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/zone_bypass/74" -m "NO"`
- Termostato 1: setpoint e modalità
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/thermostat/1/temperature" -m "21.5"`
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/thermostat/1/mode" -m "heat"`
  - `mosquitto_pub -h core-mosquitto -p 1883 -t "e-safe/cmd/thermostat/1/mode" -m "off"`

Per vedere cosa arriva:
- `mosquitto_sub -h core-mosquitto -p 1883 -v -t "e-safe/#"`
- Discovery (config retained):
  - `mosquitto_sub -h core-mosquitto -p 1883 -v -t "homeassistant/#" | grep e_safe`

## Entità E-Manager  (MQTT Discovery)
L’add-on pubblica discovery con `unique_id` stabile e `default_entity_id` per avere entità “cercabili” (es. `binary_sensor.e_safe_zone_74`).

In E-Manager , i device MQTT sono raggruppati per categoria (es. “Sensori”, “Partizioni”, “Uscite”, …).

## UI Web / Ingress
- La **webui** dell’add-on punta a `/menu` (launcher) per scegliere:
  - `Security UI`
  - `index_debug`
- L’add-on gestisce i path Ingress (prefissi `/api/hassio_ingress/...`) e riscrive i link/asset per evitare 404.

## Troubleshooting
### Entità mancanti o “strane”
1. Esegui `cleanup_discovery` (pulisce i `.../config` retained).
2. Esegui `republish_discovery` (ripubblica i `.../config` retained).
3. Riavvia l’add-on se necessario.

### MQTT device non compare
- Verifica che nei topic `homeassistant/.../config` sia presente il blocco `device`.
- Dopo modifiche discovery: sempre `cleanup_discovery` + `republish_discovery`.

### Log
I log dell’add-on sono in **Supervisor → Add-on → Log**.

## Git + sync (PC ? Samba)
- Repo GitHub: `https://github.com/edmondoalex/e-safe_ksenia_lares_4.0_addon`
- Samba (deploy HA): `\\192.168.3.24\addons\ksenia_lares_addon`

### Clone sul PC
Esempio:
- `git clone https://github.com/edmondoalex/e-safe_ksenia_lares_4.0_addon.git C:\Users\NUC Alex\workspace\e-safe_ksenia_lares_4.0_addon`

### Import automatico da Samba ? repo PC (e commit/push opzionali)
Dal repo locale:
- Solo sync file: `powershell -ExecutionPolicy Bypass -File tools\sync-samba-to-pc.ps1`
- Sync + commit: `powershell -ExecutionPolicy Bypass -File tools\sync-samba-to-pc.ps1 -AutoCommit -CommitMessage "Update"`
- Sync + commit + push: `powershell -ExecutionPolicy Bypass -File tools\sync-samba-to-pc.ps1 -AutoCommit -Push -CommitMessage "Update"`

Automazione (Windows **Operazioni pianificate**, es. ogni 1 minuto):
- `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\NUC Alex\workspace\e-safe_ksenia_lares_4.0_addon\tools\sync-samba-to-pc.ps1" -AutoCommit -Push -CommitMessage "Auto sync"`

## Note
- Audio/beep su browser: spesso richiede un tap dell’utente per sbloccare l’audio (policy browser).
- UI: ottimizzata anche per display piccoli (tablet embedded).
