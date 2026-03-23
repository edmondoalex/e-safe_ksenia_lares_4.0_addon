<INSTRUCTIONS>
## Regola obbligatoria (repo policy)
- Ogni modifica a codice/config/UI deve includere anche l’aggiornamento di `NOTES_FOR_AGENT.md` con:
  - cosa è cambiato (1–3 bullet)
  - file toccati
  - versione (`config.yaml`) se incrementata e motivo

## Convenzioni
- Preferire fix mirati e minimali, evitare refactor massivi se non richiesti.
- Mantenere naming MQTT/HA stabile (unique_id immutabile) e usare `default_entity_id` al posto di `object_id` (deprecato).
</INSTRUCTIONS>

