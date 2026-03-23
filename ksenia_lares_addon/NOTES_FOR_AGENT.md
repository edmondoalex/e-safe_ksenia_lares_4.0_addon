- Cosa e cambiato:
  - Fix sort in `prune_entity_ids` per gestire ID termostati misti (stringhe/numeri) evitando errore `'<'
    not supported between instances of 'str' and 'int'`.
- File toccati:
  - `app/debug_server.py`
- Versione (`config.yaml`):
  - non incrementata (fix interno senza modifiche di API/config).
