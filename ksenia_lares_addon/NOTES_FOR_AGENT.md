- Cosa e cambiato:
  - Fix sort in `prune_entity_ids` per gestire ID termostati misti (stringhe/numeri) evitando errore `'<'
    not supported between instances of 'str' and 'int'`.
- File toccati:
  - `app/debug_server.py`
- Versione (`config.yaml`):
  - incrementata a 5.2.71 per includere il fix sui termostati.
