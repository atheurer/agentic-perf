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
