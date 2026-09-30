# Collegamento Ekonex Platform

Questo progetto fa parte dell'ecosistema coordinato Ekonex.

Fonte condivisa ufficiale:

`../_EKONEX_PLATFORM/`

Prima di modificare identità, ruoli, API, eventi, licenze, pairing, cloud/locale/offline o sicurezza, leggere tutti i documenti indicati in `../_EKONEX_PLATFORM/README.md`.

## Regole per il Codex di questo progetto

1. Lavorare soltanto in questo progetto.
2. Non cambiare unilateralmente un contratto condiviso.
3. Usare una proposta `CHANGE-YYYY-NNN` per modifiche trasversali.
4. Conservare retrocompatibilità con le installazioni esistenti.
5. Non inserire segreti o dati cliente nei documenti.
6. A fine lavoro compilare la sezione handoff qui sotto.
7. Aggiornare soltanto la propria riga in `../_EKONEX_PLATFORM/PLATFORM_STATUS.md`.

## Handoff corrente

- Data: 2026-09-30
- Obiettivo: applicare tutte le correzioni obbligatorie della revisione coordinatore su CHANGE-2026-009 e fermarsi al commit correttivo locale.
- Risultato: versione candidata 5.2.106; aggiunti UI professionale per whitelist/classe/capability, Last Will retained `offline`, timeout correlato reale con terminale unico e conferma basata su risposta nativa affidabile.
- File modificati: `ksenia_lares_addon/app/ekonex_smarthome.py`, `ksenia_lares_addon/app/smart_home_ui.py`, `ksenia_lares_addon/app/main.py`, `ksenia_lares_addon/app/debug_server.py`, `ksenia_lares_addon/app/websocketmanager.py`, `ksenia_lares_addon/tests/__init__.py`, `ksenia_lares_addon/tests/test_ekonex_smarthome.py`, `ksenia_lares_addon/docs/EKONEX_SMART_HOME_MQTT.md`, `ksenia_lares_addon/docs/ekonex-smarthome-v1.schema.json`, `ksenia_lares_addon/README.md`, `ksenia_lares_addon/config.yaml`, `NOTES_FOR_AGENT.md`, `EKONEX_PLATFORM_SYNC.md`.
- Test eseguiti: suite pertinente completa 14/14 superata; compilazione di tutti i moduli Python superata; JSON Schema parse superato; `git diff --check` superato. Prima di CHANGE-2026-009 non erano presenti test storici automatizzati nel repository; la regressione legacy copre ora payload invariati ed esito mancante compatibile. Nessun test hardware/integrato eseguito.
- Contratti/versioni usati: MQTT Ksenia legacy invariato; Ekonex Smart Home schema 1.0 additivo; Eventi condivisi draft-1; API compatibility policy 1.0; add-on 5.2.106.
- Change ID: CHANGE-2026-009.
- Compatibilità: additiva; topic legacy, MQTT Discovery, `unique_id`, `default_entity_id`, API, porte e dati esistenti invariati; Ksenia resta autonoma senza e-Control Hub.
- Dipendenze da altri componenti: e-Control Hub deve consumare esclusivamente manifest/catalogo e pubblicare solo sui topic dichiarati.
- Attività richieste agli altri Codex: nuova revisione coordinatore del commit correttivo; successivamente test integrati producer/consumer nell'ordine previsto dalla CHANGE.
- Rischi o decisioni ancora aperte: ACL broker per-topic e risposte firmware reali non verificabili localmente; restano collaudo con centrale/broker, backup/restore operativo e test integrati al gate staging. Nessun push, installazione, deploy o release eseguito.
