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

## 2026-03-23
- Trasformato il repo in add-on repository per installazione da Git (aggiunto `repository.json`).
- Spostata la cartella add-on in `ksenia_lares_addon/` e aggiornate le istruzioni di installazione nel README.
- Nessun bump versione in `ksenia_lares_addon/config.yaml` (struttura repo, nessun cambio runtime).

File toccati:
- repository.json
- ksenia_lares_addon/README.md
- ksenia_lares_addon/app/
- ksenia_lares_addon/control4 driver/
- ksenia_lares_addon/tools/
- ksenia_lares_addon/www/
- ksenia_lares_addon/config.yaml
- ksenia_lares_addon/Dockerfile
- ksenia_lares_addon/icon.png
- ksenia_lares_addon/logo.png
- ksenia_lares_addon/run.sh
- ksenia_lares_addon/_security_page.html
- Repository Git impostato su URL reale GitHub `https://github.com/edmondoalex/e-safe_ksenia_lares_4.0_addon`.
- Aggiornato maintainer in `repository.json`.
- Nessun bump versione in `ksenia_lares_addon/config.yaml` (solo metadati repository).

File toccati:
- repository.json
- Fix repository Git per HA Add-on Store: `repository.json` riscritto in UTF-8 senza BOM (prima aveva BOM e risultava "not a valid add-on repository").
- Aggiornato campo `maintainer` con formato nome+email.
- Nessun bump versione in `ksenia_lares_addon/config.yaml` (solo metadati repository).

File toccati:
- repository.json
## 2026-03-23 - Fix domus/termostati
- Fix classificazione realtime: `STATUS_TEMPERATURES` e `STATUS_HUMIDITY` ora vengono inoltrati ai termostati solo per ID presenti in `CFG_THERMOSTATS` (evita che sensori DOMUS finiscano nei termostati in UI admin e MQTT).
- Fix merge termostati: `getThermostats()` usa ID normalizzati e, se `CFG_THERMOSTATS` esiste, limita l'elenco a quei soli ID.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.13` (bugfix classificazione domus vs termostati).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Fix domus in termostati (snapshot/UI)
- Aggiunto filtro in `LaresState` per accettare realtime termostati (`STATUS_TEMPERATURES`/`STATUS_HUMIDITY`) solo per ID presenti nei termostati statici (`CFG_THERMOSTATS`), sia in ingest iniziale che negli update realtime.
- Aggiunta normalizzazione ID termostati nel backend UI per evitare mismatch `033` vs `33`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.14` (bugfix classificazione DOMUS/termostati in snapshot e UI admin).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Fallback multi-path per ui_tags (persistenza selezione termostati)
- Aggiunta risoluzione multi-path di `ui_tags.json` con fallback (`KS_UI_TAGS_PATH`, `/data/ui_tags.json`, `/config/ui_tags.json`, `./ui_tags.json`) in `main.py`, `debug_server.py` e `websocketmanager.py`.
- Selezioni `domus_thermostats` ora lette anche quando il file non e' in `/data`, evitando lista termostati vuota in `/api/entities` per mismatch path runtime.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.69` (fix: termostati assenti nonostante `domus_thermostats` valorizzato).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Persistenza forzata ui_tags su /data
- Il salvataggio di `ui_tags.json` ora scrive sempre prima su `/data/ui_tags.json` (persistente in Home Assistant), poi replica sugli altri path candidati.
- In questo modo le selezioni `domus_thermostats` restano persistenti dopo riavvii e aggiornamenti addon.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.70` (hardening persistenza configurazione UI).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml

## 2026-04-24 - Sync totale entita statiche con centrale (prune globale)
- Estesa la sincronizzazione da `READ_DATA` a tutte le principali entita statiche (`outputs`, `zones`, `partitions`, `scenarios`, `accounts`, `domus`, `systems`, `connection`, `powerlines`) con potatura automatica degli ID rimossi.
- Aggiunto cleanup MQTT Discovery per entita rimosse (zone, uscite, partizioni, scenari, domus, utenti, sistemi, scheduler), inclusi topic legacy dove presenti.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.80` (fix: UI/add-on devono allinearsi completamente alla configurazione attuale della centrale).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md
## 2026-03-23 - Cleanup discovery termostati legacy
- Esteso `cleanup_discovery` per cancellare anche topic climate legacy `*_therm_<id>` su tutti gli ID snapshot (non solo sui termostati correnti), così rimuove i termostati rimasti da vecchia classificazione DOMUS.
- Nessun cambio a `unique_id` attuali: fix solo di pulizia retained MQTT discovery.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.15` (fix cleanup termostati legacy).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Sensor stato scenari allarme via MQTT
- Aggiunta discovery MQTT del sensore testuale `Stato Scenari allarme` su `systems`, con `value_template` da `ARM.D`.
- Aggiornato `cleanup_discovery` per includere anche il topic sensor legacy/attuale `*_sys_<id>_alarm_state`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.16` (feature: stato scenari allarme in MQTT).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Ripristino gruppo MQTT Domus
- Aggiunta discovery MQTT per entita `domus` come `sensor` (`*_domus_<id>`) con attributi JSON e raggruppamento device `Domus`.
- Aggiornato `disc_devices` con gruppo `domus` per mostrare il dispositivo `e-safe Domus` in HA.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.17` (ripristino gruppo/device Domus in MQTT discovery).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Domus: temperatura, umidita, luminosita
- Estesa discovery MQTT per `domus`: oltre al sensore stato, pubblicati sensori dedicati `temperatura`, `umidita`, `luminosita` per ogni ID DOMUS.
- Template robusti: lettura valori sia da `DOMUS.TEM/HUM/LHT` sia da root payload (`TEM/HUM/LHT`) per compatibilita'.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.18` (feature sensori Domus meteo/lux).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Domus temperatura mancante
- Fix instradamento realtime: i record `STATUS_TEMPERATURES/STATUS_HUMIDITY` non appartenenti ai termostati configurati vengono ora applicati ai `domus` (merge per ID), invece di essere scartati.
- Aggiornato template sensore Domus Temperatura con fallback multipli (`DOMUS.TEM`, `DOMUS.TEMP`, `TEM`, `TEMP`).
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.19` (bugfix temperatura Domus mancante).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Domus temperatura: publish diretto su topic dedicati
- I sensori Domus (temperatura/umidita/luminosita) ora leggono da topic dedicati (`.../domus/<id>/temperature|humidity|illuminance`) invece di template su JSON complesso.
- Durante publish `domus`, l'add-on estrae e pubblica in retain i valori da `DOMUS.TEM/TEMP`, `DOMUS.HUM`, `DOMUS.LHT`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.20` (fix temperatura Domus non valorizzata in HA).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Domus temperatura non creata in HA
- Corretto payload discovery temperatura Domus: `unit_of_measurement` da `C` a `°C` (formato valido HA per `device_class: temperature`).
- Normalizzati i valori numerici Domus pubblicati su topic dedicati (`10,6` -> `10.6`) per compatibilita' parser sensori HA.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.21` (fix creazione/lettura sensore temperatura Domus).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Termostati solo da CFG_THERMOSTATS
- Rimossa la fallback che creava termostati da ID presenti in `STATUS_TEMPERATURES/STATUS_HUMIDITY` quando mancava `CFG_THERMOSTATS`.
- Instradamento realtime reso strict: il canale `thermostats` accetta solo ID presenti in `CFG_THERMOSTATS`; gli altri record temperatura/umidita vengono trattati come Domus.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.22` (fix termostati non appartenenti alla centrale).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Domus: sensori pubblicati solo se presenti
- Discovery Domus resa condizionale: temperatura/umidita/luminosita vengono create solo se il valore e' realmente presente nel payload dell'entita (evita entita inutili per Domus senza funzione specifica).
- Mantiene il sensore base stato Domus per diagnostica (`STA`, es. `IL`).
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.23` (allineamento Domus con funzioni effettivamente disponibili).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - UI: termostati strict (no ID sbagliati)
- Backend UI (`LaresState`) reso strict per i termostati: se non ci sono ID statici noti da `CFG_THERMOSTATS`, gli update realtime `STATUS_TEMPERATURES/STATUS_HUMIDITY` non creano entita `thermostats`.
- Gli ID non appartenenti ai termostati vengono instradati su `domus` (TEM/HUM) invece che finire in `thermostats`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.24` (fix UI termostati errati).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Override Domus con termostato + nome personalizzato
- Aggiunta opzione configurabile `domus_thermostat_overrides` (JSON) per indicare quali ID Domus vanno trattati come termostati comandabili.
- Supportati ID + nome personalizzato (es. `[{"id":1,"name":"Suite 2"}]` o `{"1":"Suite 2"}`); i nomi diventano default e restano modificabili dalla UI termostati.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.25` (controllo esplicito termostati Domus e naming).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Preparazione update runtime override termostati
- Aggiunto metodo `set_extra_thermostat_names(...)` nel manager WS per aggiornare a runtime la mappa ID->nome termostati extra (base per gestione futura da UI admin).
- Nessun cambio funzionale visibile lato utente in questa singola patch (solo API interna).
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.26` (allineamento policy versione per modifica codice).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Domus termostato attivabile da UI admin
- Implementata gestione da UI admin: su ogni riga `domus` ora c'e' toggle `Thermostat` + campo nome + `Salva` (comando `domus_thermostat`).
- Persistenza in `/data/ui_tags.json` (`domus_thermostats`) con applicazione runtime immediata (`set_extra_thermostat_names`) e refresh discovery/termostati.
- Rimossa configurazione manuale `domus_thermostat_overrides` da `config.yaml`; versione incrementata a `5.2.27` per passaggio completo a gestione UI.

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Termostati solo da Domus attivati in UI
- Modalita' strict completata: la lista termostati ora include solo ID Domus abilitati da UI admin (`domus_thermostats`), senza fallback automatico da `CFG_THERMOSTATS`.
- Aggiunta potatura stato termostati non selezionati (`prune_entity_ids`) e cleanup discovery climate per rimuovere subito i termostati non piu' attivi.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.28` (richiesta utente: mostrare solo termostati attivati da Domus UI).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Setpoint termostato da UI: mapping DOMUS -> ID termostato reale
- Corretto il mapping dei termostati attivati da Domus: ora gli ID selezionati in UI vengono risolti agli ID reali `CFG_THERMOSTATS` usando `ID_TH` da `TEMPERATURES/HUMIDITY` (con fallback su match diretto CFG).
- Questo evita scritture setpoint verso ID Domus non validi e ripristina l'aggiornamento setpoint sulla centrale Ksenia.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.29` (bugfix comando setpoint UI).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Ripristino temperatura/setpoint termostati da Domus
- Corretto instradamento realtime temperatura/umidita': ora i payload con ID sensore Domus vengono mappati all'ID termostato reale tramite `ID_TH` (`TEMPERATURES`/`HUMIDITY`).
- In `getThermostats()` i dati realtime sono normalizzati su ID termostato reale e la temperatura corrente viene esposta anche quando arriva come `TEM`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.30` (bugfix: termostato senza temperatura/setpoint in UI dopo mapping strict).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Ripristino baseline produzione 5.2.60 in Git
- Allineato il contenuto dell'add-on (`ksenia_lares_addon/`) alla versione produzione 5.2.60 proveniente dal backup Samba `\\192.168.3.24\addons\_backup\ksenia_lares_addon_20260323_162341`.
- Ripristinati anche asset/script presenti solo in produzione (es. `app/www/mdi/*`, `tools/sync-samba-to-pc.ps1`) per avere in Git la stessa base in campo.
- Versione in `ksenia_lares_addon/config.yaml` riportata a `5.2.60` (baseline produzione, nessun incremento ulteriore in questa operazione).

File toccati:
- ksenia_lares_addon/config.yaml
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/Dockerfile
- ksenia_lares_addon/README.md
- ksenia_lares_addon/_security_page.html
- ksenia_lares_addon/app/www/mdi/lightbulb-group.svg
- ksenia_lares_addon/app/www/mdi/link-variant.svg
- ksenia_lares_addon/app/www/mdi/shape.svg
- ksenia_lares_addon/app/www/mdi/shield-home.svg
- ksenia_lares_addon/app/www/mdi/shield-lock.svg
- ksenia_lares_addon/app/www/mdi/window-shutter.svg
- ksenia_lares_addon/tools/sync-samba-to-pc.ps1
## 2026-03-23 - Domus: toggle termostati da UI admin (solo selezionati)
- Aggiunta gestione `domus_thermostat` da UI admin: su righe Domus ora c'e' checkbox Thermostat + nome + Salva, con persistenza in `/data/ui_tags.json` (`domus_thermostats`).
- La lista termostati (UI + MQTT) ora viene filtrata ai soli Domus attivati: mapping Domus -> termostato reale via `ID_TH`, filtro realtime e potatura entita/discovery climate non selezionate.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.61` (richiesta utente: mostrare solo termostati attivati da Domus in UI admin).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Sync termostati selezionati anche a startup/reconnect
- Aggiunto sync esplicito dei termostati selezionati Domus in fase di startup e reconnect: dopo l'ingest iniziale vengono potati i termostati non selezionati e mantenuti solo quelli attivi in `domus_thermostats`.
- Applicato anche cleanup discovery climate dei termostati rimossi durante questo sync, per evitare che restino visibili tutti dopo riavvio.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.62` (fix: dopo selezione Domus continuavano a comparire tutti i termostati).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Stop ingest termostati completi da READ iniziale
- In `LaresState._ingest_read_data` rimosso ingest diretto di `CFG_THERMOSTATS`: evitata la ricreazione automatica di tutti i termostati in UI appena parte il backend.
- I termostati ora entrano nello stato solo tramite sync filtrato (`manager.getThermostats`) gia' limitato ai Domus attivati.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.63` (fix: continuavano a comparire tutti i termostati nonostante selezione Domus).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - UI termostati: blocco creazione entita non selezionate
- In `LaresState` il canale realtime `thermostats` ora accetta aggiornamenti solo per ID termostati gia' noti/statici (quelli filtrati e selezionati), ignorando gli altri.
- Rimossa anche l'ingest iniziale di `STATUS_TEMPERATURES/STATUS_HUMIDITY` verso `thermostats` in `set_initial_data`, che ricreava termostati extra senza statico.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.64` (fix: in UI comparivano ancora tutti i termostati con static vuoto).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Fallback ID Domus per termostati selezionati senza mapping
- In risoluzione `domus_thermostats` non scarto piu' i Domus senza `ID_TH`/`CFG_THERMOSTATS`: ora uso fallback su ID Domus, cosi' i selezionati compaiono comunque in UI/MQTT.
- Mantengo comunque il mapping verso ID termostato reale quando disponibile (prioritario).
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.65` (fix: Domus selezionati ma nessun termostato visibile).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Fallback lettura selezione termostati da /data/ui_tags.json
- Aggiunta fallback nel manager WS: se la mappa runtime termostati e' vuota, legge `domus_thermostats` da `/data/ui_tags.json` e usa quella per filtrare i termostati.
- Incluso mapping automatico DOMUS->termostato reale (`ID_TH`) anche in fallback, mantenendo nome selezionato in UI.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.66` (fix: Domus selezionati ma lista termostati vuota).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Placeholder termostati selezionati (visibilita' garantita)
- Aggiunta fusione placeholder per i termostati selezionati (`domus_thermostats`): se `getThermostats()` non restituisce una voce per un ID selezionato, viene comunque creata entita statica minima (`ID`,`DES`).
- Applicato il merge in startup, reconnect, refresh da UI Domus e listener `thermostats_cfg`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.67` (fix: Domus selezionati ma nessun termostato visibile).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
## 2026-03-23 - Snapshot fallback: termostati da UI tags
- Aggiunto fallback in `LaresState.snapshot()`: se mancano entita `thermostats` runtime, vengono iniettate entita minime dai `domus_thermostats` salvati in `/data/ui_tags.json` (solo `enabled=true`).
- Questo garantisce visibilita' in UI `/thermostats` e permette discovery MQTT anche quando il sync WS non popola subito i termostati.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.68` (fix: nessun termostato visibile nonostante selezione Domus).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
## 2026-03-30 - Retry connessione MQTT in avvio
- In avvio, la connessione MQTT ora ritenta con backoff fino a 60s invece di terminare al primo errore di connessione.
- Mantengo exit immediato solo per host non risolvibile (gaierror).
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.79` (fix: add-on resta attivo finche' il broker esterno non e' pronto).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml

## 2026-05-22 - Fix avvio, publish uscite e sync programmazione termostati
- Ripristinata la visibilita' degli helper `domus_thermostats`, che prima restavano annidati in `_save_ui_tags_file` e causavano errori `not defined` in avvio/reconnect.
- Reso tollerante il parsing `POS` delle uscite quando la centrale invia valori non numerici come `NA`, evitando il blocco della pubblicazione iniziale MQTT.
- Il refresh `CFG_THERMOSTATS` ora ripubblica subito i topic MQTT `thermostats`, cosi' le modifiche di programmazione fatte su Ksenia si riallineano senza riavvio.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.81` (fix: errori avvio/publish e mancato riallineamento programmazione termostati).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml

## 2026-05-22 - Fix log eventi rumorosi e ACK scenari senza RESULT
- I timeout della lettura registro eventi (`LOGS_RES`) non vengono piu' loggati come errori vuoti: restano a livello debug.
- Le risposte `CMD_USR_RES` senza campo `RESULT` vengono trattate come ACK quando corrispondono a un comando pendente, per compatibilita' con firmware che non inviano `RESULT=OK` sugli scenari.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.82` (fix: falsi errori getLogs e scenari marcati falliti nonostante ACK).

File toccati:
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml

## 2026-05-22 - Fix comandi uscite da Security UI
- Dopo ON/OFF/TOGGLE/brightness da UI viene aggiornato e pubblicato anche lo stato `outputs`, evitando stato stale e toggle successivi errati.
- La pagina `/security/functions/outputs` usa URL API compatibili con Ingress, sblocca sempre i pulsanti con `finally` e legge l'esito JSON del comando.
- Ridotto l'impatto del poller log sulla WebSocket comandi (`LOGS` ogni 30s con timeout 3s) e allungato il timeout HTTP dei comandi UI a 65s.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.83` (fix: comandi uscite funzionavano solo una volta dopo rientro pagina).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/config.yaml

## 2026-05-24 - Preferiti: controlli ROLL come portoni/tapparelle
- Nella pagina Preferiti gli output con `CAT=ROLL` ora mostrano i comandi `UP`, `DOWN`, `STOP` invece di `ON`, `OFF`, `TOGGLE`.
- Gli output non ROLL mantengono i comandi ON/OFF/TOGGLE.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.84` (fix UI preferiti per portoni/ROLL).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml

## 2026-05-24 - Preferiti senza pulsante rimozione
- Rimosso il pulsante `Rimuovi` dalla pagina Preferiti per evitare cancellazioni accidentali.
- La gestione dei preferiti resta disponibile dalle pagine/admin dedicate.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.85` (fix UX preferiti).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml

## 2026-05-25 - Rollback lettura iniziale robusta
- Revert della modifica `5.2.86` su lettura iniziale multi-`READ_RES` e retry mirati, per evitare stati di connessione parziale con centrale Lares.
- Ripristinato il flusso WebSocket iniziale precedente, mantenendo le sole modifiche UI dei Preferiti.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.87` (rollback stabilita' connessione add-on/Lares).

File toccati:
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml

## 2026-05-25 - Rollback esteso comunicazione Lares
- Ripristinati `main.py`, `websocketmanager.py` e `wscall.py` alla base `5.2.81`, mantenendo i fix noti su helper Domus/POS `NA` ma rimuovendo le modifiche successive su ACK, poll log, comandi e sessioni WS.
- Ripristinata anche la pagina output Security UI alla logica precedente, mantenendo solo i Preferiti con `ROLL` = `UP/DOWN/STOP` e senza pulsante `Rimuovi`.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.89` (rollback stabilita' collegamento add-on/Lares dopo regressioni recenti).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-25 - Validazione lettura iniziale statica
- La `READ/MULTI_TYPES` iniziale ora accetta solo risposte `READ_RES`, smista eventuali messaggi non pertinenti al dispatcher realtime e verifica che siano presenti almeno `OUTPUTS`, `SCENARIOS`, `PARTITIONS`, `ZONES`.
- Se la prima risposta e' incompleta, viene fatto un solo retry della stessa lettura dopo 2s; se resta incompleta, l'add-on usa comunque il payload migliore ricevuto e logga le chiavi mancanti.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.90` (fix: dopo modifiche in Lares/riavvio la prima lettura poteva restare parziale, ad esempio senza scenari).

File toccati:
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-25 - Diagnostica lettura con PIN installatore
- Aggiunta opzione `installer_pin` opzionale: viene usata solo all'avvio per una sessione diagnostica separata con login `PAYLOAD_TYPE=INSTALLER`, seguendo il modello del driver Control4.
- La diagnostica legge `READ/PAYLOAD_TYPE=ALL` con `ID_ITEMS_RANGE=["ALL","ALL"]`, confronta i conteggi statici con la lettura normale e logga se il PIN installatore vede piu' elementi.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.91` (diagnostica permessi/visibilita' dati dopo modifiche in Lares).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/config.yaml
- ksenia_lares_addon/README.md
- NOTES_FOR_AGENT.md

## 2026-05-25 - Diagnostica installer non bloccante
- La diagnostica `installer_pin` viene avviata in background dopo la pubblicazione discovery, cosi' un timeout della lettura installer non ritarda l'avvio operativo.
- Ridotto il timeout `READ ALL` installer a 6s e declassati i timeout a warning, perche' il log utente mostra lettura normale gia' completa ma `READ ALL` installer non risponde.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.92` (fix: diagnostica installer non deve bloccare startup/allineamento).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-25 - Diagnostica installer su PRG specifici
- Rimossa dalla diagnostica la lettura installer `READ/PAYLOAD_TYPE=ALL`, che sulla centrale utente va in timeout.
- Aggiunte letture diagnostiche `PRG_ZONES`, `PRG_PARTITIONS`, `PRG_OUTPUTS` con `ID_ITEMS_RANGE=["ALL","ALL"]`, come sequenza usata dal driver Control4.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.93` (diagnostica installer mirata senza `READ ALL`).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-25 - Rimozione pulsante refresh flottante Security UI
- Rimossi i pulsanti flottanti `refreshBtn` con icona refresh dalle pagine Security UI, evitando il controllo visibile in alto/laterale.
- Lasciati invariati i pulsanti `Aggiorna` interni alle pagine elenco/preferiti.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.94` (fix UI richiesto dall'utente).

File toccati:
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/_security_page.html
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - Alleggerimento WebSocket e polling log
- Disattivato il keepalive automatico della libreria `websockets` sulle connessioni verso Lares, usando il controllo manuale gia' presente nel listener per evitare chiusure `1011 keepalive ping timeout`.
- Ridotto il polling del registro eventi da 500 log ogni 5s a 150 log ogni 60s, con timeout piu' breve e log a warning sui timeout.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.95` (fix: ridurre carico e falsi errori durante rallentamenti/riavvii Lares).

File toccati:
- ksenia_lares_addon/app/websocketmanager.py
- ksenia_lares_addon/app/wscall.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - Ricevitore SIA-IP addon
- Aggiunto ricevitore TCP SIA-IP opzionale nell'addon, con ACK base DC-09, parser eventi non cifrati, filtro account, storico eventi e stato aggregato allarmi/trouble.
- Aggiunta pagina `Funzioni -> SIA-IP` nella UI security e sensori MQTT discovery per stato, ultimo evento/codice/zona, allarmi e trouble SIA-IP.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.96` (feature: ricezione eventi SIA-IP da Lares e pubblicazione MQTT/HA).

File toccati:
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - ACK SIA-IP con routing Lares
- Il parser SIA-IP ora conserva il routing DC-09 tra sequence e account (es. `R000001L010000`) e lo riecheggia nell'ACK.
- Fix per eventi ricevuti correttamente ma marcati da Lares come `Segnalazione SIA fallita` per ACK non conforme al frame ricevuto.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.97` (fix ACK SIA-IP per receiver/line routing Lares).

File toccati:
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - CRC ACK SIA-IP Lares
- Corretto il CRC dei frame ACK SIA-IP da CCITT a CRC-16/IBM (`0xA001`, init `0x0000`), variante verificata sui frame reali Lares.
- L'ACK ora mantiene routing/account e calcola un CRC coerente con i pacchetti in ingresso, evitando scarti per checksum non valido.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.98` (fix: Lares riceveva ACK ma continuava a segnare segnalazione SIA fallita).

File toccati:
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - Parsing qualificatore SIA-DCS
- Il parser SIA-IP ora riconosce eventi SIA-DCS con qualificatore iniziale, es. `NJP1^installatore^` e `NRP`, estraendo `qualifier`, `code`, identificativo e utente.
- Aggiunte descrizioni base per `JP` (accesso utente) e mantenuto `RP` come test comunicazione.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.99` (fix: eventi SIA ricevuti ma mostrati come `unknown` in UI/MQTT).

File toccati:
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - Sensori SIA-IP arricchiti con nomi Lares
- Gli eventi SIA-IP vengono arricchiti usando le anagrafiche gia' lette via WebSocket: nome zona, nome partizione e nome account/utente quando disponibili.
- Aggiunti sensori MQTT testuali per `Inserito da`, `Disinserito da`, `Zona in allarme`, `Partizione in allarme`, ultima zona/partizione per nome e stato Lares aggregato.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.100` (feature: sensori SIA-IP leggibili con nomi Lares invece di soli codici/ID).

File toccati:
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - Parsing SIA CL/OP come partizioni
- Corretto parsing SIA-DCS: `CL`, `OP` e `UX` usano l'identificativo come partizione/area, non come utente o zona.
- Aggiunto mapping `UX` come `Inserimento ritardato`, coerente con il ricevitore SIA esterno usato per confronto.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.101` (fix: sensori inserimento/disinserimento e partizione SIA usavano ID errati).

File toccati:
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/app/main.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md

## 2026-05-26 - Mappatura SIA Ksenia da PDF ufficiale
- Esteso il dizionario SIA-IP con i codici Ksenia DC-09 dal PDF `Tabelle mappatura eventi Ksenia per codici SIA DC-09 e Contact ID.pdf` (zone, partizioni, periferiche, comunicazioni, alimentazione, scheda madre).
- Aggiornata la gestione stato attivo allarmi/trouble per includere i nuovi codici allarme, ripristino e guasto.
- Versione incrementata in `ksenia_lares_addon/config.yaml` a `5.2.102` (feature: interpretazione eventi SIA piu' completa e coerente con tabella Ksenia).

File toccati:
- ksenia_lares_addon/app/siaip.py
- ksenia_lares_addon/app/debug_server.py
- ksenia_lares_addon/config.yaml
- NOTES_FOR_AGENT.md
