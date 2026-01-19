# Notes for agent (Ksenia Lares MQTT Add-on)

Questo file serve a riprendere velocemente il contesto quando si riapre VS Code / una nuova sessione.

## Repo
- Percorso: `\\192.168.3.24\addons\ksenia_lares_addon`
- Add-on Home Assistant (Supervisor) + UI Ingress + MQTT Discovery.
- Non è un repo git (niente `git log`).
- Policy: vedi `AGENTS.md` (obbligo aggiornare questo file ad ogni modifica).

## Versioning
- La versione add-on è in `config.yaml` (`version:`).
- La UI legge la versione da `ADDON_VERSION` env (se presente) oppure da `config.yaml` copiato nel container.
- `Dockerfile` copia `config.yaml` dentro l’immagine (`/app/config.yaml`) per avere versione coerente in UI.

## MQTT (prefix = `options.mqtt_prefix`, default `ksenia`)
### Comandi (subscribe)
- `.../cmd/output/<id>`: ON/OFF per outputs.
- `.../cmd/scenario/<id>`: esegue scenario.
- `.../cmd/thermostat/<id>/mode|preset_mode|temperature`: set hvac/preset/target temp.
- `.../cmd/scheduler/<id>`: abilita/disabilita programmatore (EN).
- `.../cmd/account/<id>`: abilita/disabilita account.

### ACK (publish)
- `.../ack/output/<id>`
- `.../ack/scenario/<id>`
- `.../ack/thermostat/<id>`
- `.../ack/scheduler/<id>`
- `.../ack/account/<id>`
- `.../ack/panel/<action>` (reset rapidi)

### Discovery (Home Assistant)
Pubblicato sotto `homeassistant/<domain>/<object_id>/config` (retain=true).

Entità principali (tutte con `unique_id` stabile):
- Zones: `binary_sensor.e_safe_zone_<id>` + sensori extra (Allarme/BYP/Tamper/Mask)
  - Bypass R/W: switch comando su `.../cmd/zone_bypass/<id>` (naming può variare).
- Partitions: `alarm_control_panel.e_safe_part_<id>` (R/W), `code_arm_required: false`.
- Outputs: `switch.e_safe_out_<id>` (R/W).
- Scenarios: MQTT `button` (non `script`, perché MQTT discovery non supporta `script` domain).
- Schedulers: `switch.e_safe_sched_<id>` (EN T/F) (R/W).
- Thermostats: `climate.e_safe_therm_<id>` + topic derivati per stato robusto.
- Accounts: `switch.e_safe_user_<id>` (prefisso “e_safe_user_” per cercarli facilmente).

Nota: evitare `object_id` in discovery (deprecato HA 2026.4+). Preferire `default_entity_id` se serve.

## UI (Ingress)
- Index debug: `/index_debug` (porta container 8080, host 18888).
- Security UI: `/security` (porta container 8081, host 18889).
- Launcher menu: `/menu` (usato come `webui` per scegliere UI).
- Menu: usa link e asset relativi (`security`, `index_debug`, `assets/...`) per funzionare in Ingress senza dipendere dal rewrite.

## index_debug (filtri)
- I tipi mostrati in `index_debug` sono memorizzati in `localStorage` (`ksenia_index_debug_types`).
- Default: `memoria_allarmi` disattivato (meno rumore), riattivabile dalla lista tipi.

## UI Security (responsive)
- `/security` usa variabili CSS + `clamp()`/breakpoint per adattarsi fino a `480x320` (portrait/landscape): ring, topbar, padding e testi scalano; su schermi bassi la `hint` viene nascosta; spazi verticali (stato/scenario) ridotti con variabili; su `max-height:420px` e `max-height:360px` riduce ulteriormente ring/lock/topbar/font per mantenere visibile lo scenario senza scroll.
- Ring “fit”: `--ring-size` è calcolato anche in funzione di `100vh` (`calc(100vh - topbar - ... )`) per evitare che su tablet embedded (es. Control4) lo scenario finisca sotto e richieda scroll.
- Ingress: `ingress_entry` punta a `/menu` e su porta 8080 `/` fa redirect a `/menu` (così anche “Add to sidebar” apre il launcher).
- Nota: su porta 8080 NON si redirecta `/index_debug`, altrimenti dal menu non si riesce ad aprire `index_debug` via Ingress.
- Ingress routing: il server accetta anche path prefissati tipo `/api/hassio_ingress/<token>/...` e `/local_<slug>/ingress/...` (li normalizza), e la UI inietta uno shim JS che prefissa i link assoluti (`/security`, `/assets/...`) col root Ingress.
- Ingress shim: lo script iniettato riscrive anche `img/src` e osserva il DOM (MutationObserver) per evitare 404 quando la pagina carica elementi dopo l’esecuzione iniziale.
- Ingress shim: `prefix()` è idempotente (evita prefissi ripetuti tipo `/api/hassio_ingress/.../api/hassio_ingress/...`) e il MutationObserver è throttled per non creare loop.
- Logo `e-safe_scr.png`:
  - NON in topbar della home sicurezza.
  - Presente dentro le pagine Security UI (sensori, partizioni, scenari, output, programmatori, reset, info, utenti, registro).

## Bugfix importanti fatti
### 1) Entità non trovabili per prefisso / unique_id / duplicati
- Molto lavoro su MQTT discovery per mantenere `unique_id` stabile e naming coerente.
- Accounts/Users creati con prefisso cercabile `e_safe_user_...`.

### 2) Partitions discovery error “required key command_topic”
- Sistemato: `alarm_control_panel` richiede `command_topic` (e impostazioni R/W).

### 3) Thermostat “Invalid modes mode: MAN”
- Mapping hvac_mode/preset_mode corretto.
- Topic derivati retained per evitare template che “balla”.

### 4) Scenarios non comparivano
- Sistemato convertendo in `button` MQTT + command_topic.

### 5) Schedulers (programmatori) non pubblicati
- Aggiunta discovery e comando `switch` EN.

### 6) “Ultimi movimenti” pagina Sensori e persistenza
- `last_seen` delle zone ora:
  - aggiorna solo su “evento vero” (cambio campi: `STA`, `BYP`, `T`, `VAS`, `FM`, `A`)
  - persiste su `/data/last_seen_zones.json` (flush ~ ogni 5s)
  - ID normalizzato (evita duplicati int vs str che bloccavano lo stato in allarme)
- Ordinamento “Data” in pagina Sensori: per ultimo evento (desc), non più “allarmi prima”.
- Fix timezone: ora “ultimo evento” usa timestamp server + offset della centrale (o fallback browser), senza drift/delta.
- Fix riavvio: al primo realtime dopo reboot non sovrascrive lo storico (evita reset ordine “Data”).

### 7) Deprecation HA 2026.4+ (object_id)
- Rimosso `object_id` dai payload discovery pubblicati dall’add-on; usato `default_entity_id`.

### 8) Security UI su tablet piccoli (Control4)
- Ridotto leggermente `--ring-size` e spaziatura scenario per evitare scroll e rendere visibile la scritta sotto il cerchio (v5.1.13).
- Reso il tweak solo per schermi piccoli (media query), per non ridurre la UI sui tablet grandi (v5.1.14).

### 9) Device MQTT (raggruppamento entità in un dispositivo)
- Aggiunto blocco `device` nei payload di MQTT discovery per far comparire un dispositivo `e-safe` nella pagina MQTT (v5.1.15).
- Modalità “per categoria”: separati device MQTT per `zones/partitions/outputs/scenarios/thermostats/schedulers/accounts/systems/panel` (v5.1.16).
- UI: bottone “Aggiorna stato” ora usa icona SVG (niente `?`) per compatibilità sui tablet (v5.1.17).
- Add-on: icona Home Assistant aggiornata usando `www/e-safe alarm.png` come `icon.png` e `logo.png` (v5.1.18).
- Add-on: nome mostrato in HA aggiornato a `e-Safe Ksenia Lares 4.0` (v5.1.19).
- Add-on: aggiunta documentazione dettagliata in `README.md` mostrata nella pagina info di Home Assistant (v5.1.20).
- README: aggiunti “Quick start” + esempi `mosquitto_pub/mosquitto_sub` copia-incolla (v5.1.21).
- Licenza: rimosso sistema licenza offline + tool + dipendenza `ecdsa` (v5.2.1).
- UI: fallback CSS per non restare bloccati sullo splash (logo) su browser/tablet vecchi (v5.2.2).
- UI: tentativo loader JS legacy rimosso (rompeva f-string in `debug_server.py`); tenuto solo fallback CSS splash per tablet/browser vecchi (v5.2.4).
- WS: fix aggiornamento stati UI/index_debug quando i poller (logs/zones/schedulers/thermostats_cfg) consumavano messaggi realtime mentre aspettavano le risposte (v5.2.5).
  - File: `app/wscall.py`, `app/websocketmanager.py`
  - Versione: `config.yaml` -> 5.2.5 (bugfix perdita update stati)
- Log: aggiunti log mirati per debug uscite (update WS1 e comandi MQTT cmd/output) sotto `output_debug_verbose` (v5.2.6) e poi resi indipendenti da `mqtt_debug_verbose` (v5.2.7); aggiunto log lato WS manager per confermare arrivo `STATUS_OUTPUTS` (v5.2.8).
  - File: `app/main.py`, `app/websocketmanager.py`
  - Versione: `config.yaml` -> 5.2.8 (debugging + log STATUS_OUTPUTS)
- WS: hardening listener (non deve morire su payload inattesi, specie durante modifiche programmazione da centrale) (v5.2.9).
  - File: `app/websocketmanager.py`
  - Versione: `config.yaml` -> 5.2.9 (bugfix freeze)
- WS: fix reconnect dopo modifiche programmazione: chiusura WS sempre serializzata + close su login fallito + guardie poller quando WS è None + delay breve dopo close code=1000 "Bye..." (v5.2.10).
  - File: `app/websocketmanager.py`
  - Versione: `config.yaml` -> 5.2.10 (bugfix reconnect / NoneType.send)
- WS: reconnect autonomo quando la centrale butta giù le WS durante modifiche programmazione: cooldown dopo close code=1000 ("Bye...") + retry infinito con backoff e reset retry ad ogni tentativo (v5.2.11).
  - File: `app/websocketmanager.py`
  - Versione: `config.yaml` -> 5.2.11 (bugfix autoreconnect)
- WS: cooldown reconnessione configurabile (`ws_reconnect_cooldown_sec`, default 8s) per impianti che tornano online in 3–7s ma possono impiegare di più (v5.2.12).
  - File: `config.yaml`, `app/main.py`, `app/websocketmanager.py`
  - Versione: `config.yaml` -> 5.2.12 (tuning)

## File principali
- `app/main.py`: MQTT, discovery, cmd handler, republish/cleanup discovery.
- `app/websocketmanager.py`: WS verso centrale, listener realtime/static, comandi (write cfg).
- `app/wscall.py`: helper chiamate WS (read zones ecc).
- `app/debug_server.py`: UI ingress + LaresState + snapshot + persistenza last_seen zones.
- `config.yaml`: versione e opzioni add-on.
- `Dockerfile`: copia `config.yaml` nel container.

## Workflow pratico (quando “spariscono” o cambiano entità in HA)
- Usare endpoint add-on (o pulsante in UI) per:
  - `cleanup_discovery` (pulisce config retained) poi
  - `republish_discovery` (ripubblica config retained)
- Riavviare add-on se necessario.

## Git / deploy (consigliato)
- Repo “source of truth” su PC (GitHub): `C:\Users\NUC Alex\OneDrive\EA SAS\0000000033-TOOL\HASSIO ADDON\Ksenia lares`
- Deploy su Home Assistant (Samba): `\\192.168.3.24\addons\ksenia_lares_addon`
- Regola: lavorare/committare sul repo PC, poi sincronizzare verso Samba per installare/aggiornare l’add-on (così versioni e sorgenti restano allineati).
- Script: `tools/sync-addon.ps1` (robocopy) per sync PC → Samba (e/o prima import Samba → PC).
- Script: `tools/bootstrap-git.ps1` per inizializzare/configurare Git remoto e fare `commit/push` dal repo su PC.

## Come tenere aggiornato questo file
- Dopo ogni sessione: aggiungere una riga in “Bugfix importanti fatti” o “TODO” con:
  - cosa è stato cambiato
  - file toccati
  - versione (`config.yaml`) e motivo del bump

## TODO / idee future
- Audio beep in UI: richiede almeno 1 tap per sbloccare audio (policy browser).
- Event log/cronologia “ultimi N eventi per zona” (oltre all’ultimo evento).
- Pulizia completa di eventuali discovery legacy rimasti (old object_id / vecchi domini).
