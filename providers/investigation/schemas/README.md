# Investigation Record Schemas

JSON Schema files generated from the Pydantic models in
`providers/investigation/models.py`. These are the definitive
reference for backend implementers (OpenSearch, Elasticsearch,
Horreum, PostgreSQL, etc.).

## Files

| File | Schema URI | Description |
|---|---|---|
| `investigation-record-v1.json` | `urn:agentic-perf:investigation-record:v1` | Current record schema |

## Regenerating

Schemas are generated from the Pydantic models. To regenerate
after model changes:

```bash
python3 -c "
import json
from providers.investigation.models import InvestigationRecord
schema = InvestigationRecord.model_json_schema()
with open('providers/investigation/schemas/investigation-record-v1.json', 'w') as f:
    json.dump(schema, f, indent=2)
    f.write('\n')
"
```

Bump the version in `models.py` (`SCHEMA_VERSION`,
`SCHEMA_URI`) and create a new schema file when making
breaking changes to the record structure.

## Horreum Backend Setup

When using Horreum as the investigation records backend,
the following setup is required:

### Schema

Upload `investigation-record-v1.json` as a Horreum schema
with URI `urn:agentic-perf:investigation-record:v1`.

### Labels

Create the following labels on the schema for server-side
filtering. These are required for deterministic dedup
matching and efficient queries.

| Label | JSON Path | Type | Purpose |
|---|---|---|---|
| `state` | `$.state` | String | Filter open vs closed records |
| `metric` | `$.anomaly_context.metric` | String | Dedup matching by metric name |
| `platform` | `$.anomaly_context.platform` | String | Dedup matching by target platform |

Without these labels, the provider falls back to fetching
all records and filtering in Python, which works but makes
N+1 HTTP calls per query.

### Test

Create a Horreum test owned by your team. Set `test_id` in
the `investigation_records` config:

```json
{
    "investigation_records": {
        "backend": "horreum",
        "url": "https://horreum.example.com",
        "secret": "horreum/api-key",
        "test_id": 426
    }
}
```

### Dedup Key

For deterministic dedup to work, records must have `metric`
and `platform` populated in `anomaly_context`. These are
set automatically by the synthesis agent from the ticket's
`dedup_key` field (written by webhook enrichment or ticket
creation). Records with empty `metric`/`platform` will not
match deterministic dedup and fall through to LLM matching.
