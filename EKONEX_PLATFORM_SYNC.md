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
- Obiettivo: implementare il producer Ksenia Smart Home autorizzato da CHANGE-2026-009 fino al commit locale.
- Risultato: versione candidata 5.2.105; manifest/catalogo whitelist-only retained, ID persistenti, mapping ai topic legacy ed esiti comando correlati non retained implementati. Domini sicurezza esclusi in hard-fail.
- File modificati: `ksenia_lares_addon/app/ekonex_smarthome.py`, `ksenia_lares_addon/app/main.py`, `ksenia_lares_addon/tests/test_ekonex_smarthome.py`, `ksenia_lares_addon/docs/EKONEX_SMART_HOME_MQTT.md`, `ksenia_lares_addon/docs/ekonex-smarthome-v1.schema.json`, `ksenia_lares_addon/README.md`, `ksenia_lares_addon/config.yaml`, `NOTES_FOR_AGENT.md`, `EKONEX_PLATFORM_SYNC.md`.
- Test eseguiti: 7 unit test contratto/negativi superati; compilazione Python completa superata; JSON Schema parse superato; `git diff --check` superato. Nessun test hardware/integrato o deploy eseguito.
- Contratti/versioni usati: MQTT Ksenia legacy invariato; Ekonex Smart Home schema 1.0; Eventi condivisi draft-1; API compatibility policy 1.0; add-on 5.2.105.
- Change ID: CHANGE-2026-009.
- Compatibilità: additiva; topic legacy, MQTT Discovery, `unique_id`, `default_entity_id`, API, porte e dati esistenti invariati; Ksenia resta autonoma senza e-Control Hub.
- Dipendenze da altri componenti: e-Control Hub deve consumare esclusivamente manifest/catalogo e pubblicare solo sui topic dichiarati.
- Attività richieste agli altri Codex: revisione coordinatore; implementazione consumer capability-based e test integrati nell'ordine previsto dalla CHANGE.
- Rischi o decisioni ancora aperte: ACL broker per-topic non verificabile localmente; collaudo con centrale/broker reali, backup/restore operativo e regressione completa add-on restano al gate staging. Nessun push, installazione, deploy o release eseguito.
