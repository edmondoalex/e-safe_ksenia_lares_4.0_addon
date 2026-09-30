# Ekonex Smart Home MQTT contract

Schema version: `1.0`. This additive contract exposes only objects explicitly enabled in `ui_tags.json`; Home Assistant Discovery and all legacy state/command topics remain unchanged.

The machine-readable schema is [`ekonex-smarthome-v1.schema.json`](ekonex-smarthome-v1.schema.json). Consumers must ignore unknown fields for forward-compatible additive changes.

## Explicit whitelist

An object is exported only when its existing `ui_tags.json` entry contains `smart_home.enabled: true`. `visible`, `favorite` and tags never enable export implicitly.

```json
{
  "outputs": {
    "7": {
      "tag": "Luci",
      "smart_home": {
        "enabled": true,
        "class": "dimmer",
        "capabilities": ["on", "off", "level"]
      }
    }
  },
  "ekonex_smarthome": {
    "device_ids": {
      "outputs:7": "ksn_6b93877ff7284e5a8af83a590d30ca48"
    }
  }
}
```

The persisted `device_ids` map is part of the same backed-up configuration as `ui_tags.json`, so IDs survive rename, restart and restore.

Allowed native families are `outputs`, `scenarios`, `domus` and `thermostats`. Partitions, zone bypass, accounts/PINs, panel/reset, SIA-IP and all other security data are rejected and never appear in the catalog.

## Coordination topics

- `ekonex/v1/integrations/ksenia/manifest` — retained
- `ekonex/v1/integrations/ksenia/devices` — retained
- `ekonex/v1/integrations/ksenia/command_result/<command_id>` — not retained

Example manifest:

```json
{
  "schema_version": "1.0",
  "integration_id": "ksenia",
  "display_name": "Ksenia Smart Home",
  "availability_topic": "e-safe/status",
  "mqtt_prefix": "e-safe",
  "capabilities": ["level", "off", "on"],
  "catalog_topic": "ekonex/v1/integrations/ksenia/devices",
  "command_result_topic_template": "ekonex/v1/integrations/ksenia/command_result/{command_id}"
}
```

Example catalog item:

```json
{
  "device_id": "ksn_6b93877ff7284e5a8af83a590d30ca48",
  "native_type": "outputs",
  "native_id": "7",
  "name": "Kitchen",
  "class": "dimmer",
  "capabilities": ["level", "off", "on"],
  "state_topic": "e-safe/lights/7",
  "command_topic": "e-safe/cmd/output/7",
  "payload_format": "legacy_or_ekonex_envelope_v1",
  "read_only": false,
  "enabled": true,
  "source": "ksenia"
}
```

The producer supplies every state and command topic. Consumers must not reconstruct them. Thermostats additionally declare the exact `command_topics` for `temperature`, `mode` and `preset`.

## Correlated commands

e-Control Hub publishes the envelope on the catalogued legacy command topic:

```json
{
  "command_id": "cmd_01K...",
  "correlation_id": "cor_01K...",
  "payload": "ON"
}
```

Ksenia validates the exact topic, declared capability and limits, extracts `payload`, and invokes the same legacy command implementation. Plain legacy payloads such as `ON`, `42`, `OPEN` or `EXECUTE` continue to work unchanged.

Results use stable statuses `accepted`, `confirmed`, `failed`, `timeout` and `unavailable`:

```json
{
  "schema_version": "1.0",
  "command_id": "cmd_01K...",
  "correlation_id": "cor_01K...",
  "status": "confirmed",
  "timestamp": 1790784000
}
```

An envelope targeting an absent/non-whitelisted topic fails with `topic_not_catalogued`. Invalid capabilities, payloads and limits fail before dispatch. State remains authoritative on the existing Ksenia state topic; a command result is not an optimistic state update.

## Operational security

Use a dedicated MQTT identity for e-Control Hub and broker ACLs that permit subscribe to manifest/catalog/whitelisted state topics and publish only to catalogued Smart Home command topics. The application allowlist is hard-fail, but it does not replace broker ACLs.
