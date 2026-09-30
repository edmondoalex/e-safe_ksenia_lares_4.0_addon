"""Small, dependency-free renderer for the Ksenia Smart Home export UI."""

from __future__ import annotations

import html
import json


CLASSES = {
    "outputs": {
        "switch": ["on", "off", "toggle"],
        "light": ["on", "off", "toggle"],
        "dimmer": ["on", "off", "toggle", "level"],
        "cover": ["open", "close", "stop", "position"],
        "gate": ["open", "close", "stop"],
        "garage_door": ["open", "close", "stop"],
        "awning": ["open", "close", "stop", "position"],
        "shutter": ["open", "close", "stop", "position"],
    },
    "scenarios": {"scenario": ["execute"]},
    "domus": {
        "environment_sensor": ["temperature", "humidity", "illuminance"],
        "temperature_sensor": ["temperature"],
        "humidity_sensor": ["humidity"],
        "illuminance_sensor": ["illuminance"],
    },
    "thermostats": {"thermostat": ["temperature", "mode", "preset"]},
}


def _norm_id(value):
    try:
        return str(int(str(value).strip()))
    except Exception:
        return str(value or "").strip()


def _name(entity, fallback):
    static = entity.get("static") if isinstance(entity.get("static"), dict) else {}
    return str(static.get("DES") or static.get("NM") or entity.get("name") or fallback).strip()


def render_smart_home_exports(snapshot: dict, ui_tags: dict) -> bytes:
    entities = snapshot.get("entities") if isinstance(snapshot, dict) else []
    rows = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        native_type = str(entity.get("type") or "").lower()
        native_id = _norm_id(entity.get("id"))
        if native_type not in CLASSES or not native_id:
            continue
        entry = (ui_tags.get(native_type) or {}).get(native_id) if isinstance(ui_tags, dict) else None
        export = entry.get("smart_home") if isinstance(entry, dict) else None
        export = export if isinstance(export, dict) else {}
        rows.append(
            {
                "type": native_type,
                "id": native_id,
                "name": _name(entity, f"{native_type} {native_id}"),
                "enabled": export.get("enabled") is True,
                "class": str(export.get("class") or ""),
                "capabilities": export.get("capabilities") if isinstance(export.get("capabilities"), list) else [],
            }
        )
    rows.sort(key=lambda row: (row["type"], row["name"].casefold(), row["id"]))
    rows_json = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    classes_json = json.dumps(CLASSES, ensure_ascii=False)
    body_rows = "".join(
        f'<tr data-type="{html.escape(row["type"])}" data-id="{html.escape(row["id"])}">'
        f'<td><strong>{html.escape(row["name"])}</strong><small>{html.escape(row["type"])} · ID {html.escape(row["id"])}</small></td>'
        '<td><label class="toggle"><input class="enabled" type="checkbox"><span>Esporta</span></label></td>'
        '<td><select class="device-class" aria-label="Classe Smart Home"></select></td>'
        '<td><div class="capabilities"></div></td>'
        '<td><button class="save" type="button">Salva</button><span class="result" aria-live="polite"></span></td></tr>'
        for row in rows
    ) or '<tr><td colspan="5">Nessun oggetto Smart Home disponibile.</td></tr>'
    page = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ksenia · Export Smart Home</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#0b1018;color:#eef2f7}}main{{max-width:1180px;margin:auto;padding:20px}}
a{{color:#7fc4ff}}.hint,small{{display:block;color:#aab5c4;font-size:12px}}table{{width:100%;border-collapse:collapse;margin-top:18px;background:#121a25}}
th,td{{padding:12px;border-bottom:1px solid #293443;text-align:left;vertical-align:top}}select,button{{padding:8px;border-radius:7px}}
.capabilities{{display:flex;flex-wrap:wrap;gap:9px}}.capabilities label{{white-space:nowrap}}.result{{display:block;font-size:12px;margin-top:5px}}
@media(max-width:760px){{table,tbody,tr,td{{display:block}}thead{{display:none}}tr{{border:1px solid #293443;margin:12px 0}}}}
</style></head><body><main>
<a href="menu">← Menu</a><h1>Export Ekonex Smart Home</h1>
<p class="hint">L'export è sempre esplicito. Visibilità, preferiti e tag non abilitano alcun oggetto. Partizioni, zone, account, panel e SIA-IP non sono selezionabili.</p>
<table id="exports"><thead><tr><th>Oggetto</th><th>Export</th><th>Classe</th><th>Capability</th><th>Azione</th></tr></thead><tbody>{body_rows}</tbody></table>
<script>
const rows={rows_json}; const classes={classes_json};
function renderCapabilities(tr, selected){{
  const type=tr.dataset.type, cls=tr.querySelector('.device-class').value;
  const box=tr.querySelector('.capabilities'); box.innerHTML='';
  for(const cap of ((classes[type]||{{}})[cls]||[])){{
    const label=document.createElement('label'), input=document.createElement('input'); input.type='checkbox'; input.value=cap;
    input.checked=selected.includes(cap); label.append(input,document.createTextNode(' '+cap)); box.append(label);
  }}
}}
for(const row of rows){{
  const tr=document.querySelector(`tr[data-type="${{row.type}}"][data-id="${{row.id}}"]`); if(!tr)continue;
  tr.querySelector('.enabled').checked=row.enabled; const sel=tr.querySelector('.device-class');
  for(const cls of Object.keys(classes[row.type]||{{}})){{const o=document.createElement('option');o.value=cls;o.textContent=cls;sel.append(o)}}
  sel.value=row.class||sel.options[0]?.value||''; renderCapabilities(tr,row.capabilities); sel.onchange=()=>renderCapabilities(tr,[]);
  tr.querySelector('.save').onclick=async()=>{{
    const result=tr.querySelector('.result'); result.textContent='Salvataggio…';
    const value={{target_type:row.type,smart_home:{{enabled:tr.querySelector('.enabled').checked,class:sel.value,capabilities:[...tr.querySelectorAll('.capabilities input:checked')].map(x=>x.value)}}}};
    try{{const res=await fetch('api/cmd',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{type:'ui_tags',id:Number(row.id),action:'set',value}})}});const data=await res.json();if(!res.ok||!data.ok)throw new Error(data.error||'save_failed');result.textContent='Salvato';}}
    catch(err){{result.textContent='Errore: '+(err.message||err);}}
  }};
}}
</script></main></body></html>"""
    return page.encode("utf-8")
