# QUADS Self-Service Host Reservation

QUADS (Quick and Dirty Scheduler) manages bare-metal lab host reservations.
The resource agent uses this skill when `resource_provider: quads` is requested.

## Configuration

Credentials and endpoint are in `~/.agentic-perf/secrets/quads/config.json`:
```
api_host   — QUADS server hostname (e.g. quads.rdu2.scalelab.redhat.com)
email      — login email
password   — login password
owner      — username portion of email (before @)
ssh_key_path — SSH key to inject into reserved hosts
```

**Note:** The `api_host` in the config file may differ from the host used to
query individual host records. Always query host availability using
`quads.rdu2.scalelab.redhat.com` (no "2"). Use the `api_host` from config
for authenticated operations.

## Full Reservation Flow

### 1. Login — get Bearer token

```bash
TOKEN=$(curl -s -k -X POST \
  -u "$EMAIL:$PASSWORD" \
  -H "Content-Type: application/json" \
  "https://$API_HOST/api/v3/login/" \
  | awk -F: '{print $2}' | awk -F, '{print $1}' | sed -e 's/^"//' -e 's/"$//')
```

All subsequent write requests require `-H "Authorization: Bearer $TOKEN"`.

### 2. Check host availability

```bash
curl -s "https://quads.rdu2.scalelab.redhat.com/api/v3/hosts/<hostname>/" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print('cloud:', d['cloud']['name'], 'can_self_schedule:', d['can_self_schedule'])"
```

A host is available when `cloud.name == "cloud01"` and `can_self_schedule == true`.

### 3. Create assignment (auto-selects a cloud slot)

```bash
curl -s -k -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description": "short description", "owner": "<owner>", "qinq": 0, "wipe": "true"}' \
  "https://$API_HOST/api/v3/assignments/self"
```

Response includes `cloud.name` (e.g. `cloud23`) and `id` (assignment ID).
The `wipe: true` flag reprovisiones the host with the selected OS.

### 4. Schedule each host into the assignment

```bash
curl -s -k -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cloud": "<cloud_name>", "hostname": "<fqdn>"}' \
  "https://$API_HOST/api/v3/schedules"
```

Repeat for each host. The default reservation end time is set by the QUADS
`ssm_default_lifetime` config (typically 3 days from now).

### 5. Poll for validation

```bash
curl -s "https://$API_HOST/api/v3/assignments/<assignment_id>/" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print('provisioned:', d['provisioned'], 'validated:', d['validated'])"
```

Poll every 60 seconds. Hosts are ready when `validated == true`.
Provisioning typically takes 20-40 minutes (OS install + reboot).

### 6. Get assigned host IPs/FQDNs

After validation, query each host's current cloud to confirm assignment,
then use the FQDNs directly — QUADS hosts are reachable by name.

### 7. Terminate when done

```bash
curl -s -k -X POST \
  -H "Authorization: Bearer $TOKEN" \
  "https://$API_HOST/api/v3/assignments/terminate/<assignment_id>"
```

## Key details

- `owner` field must be the username portion of the email (before `@`)
- `cloud01` is the QUADS free pool; assigned clouds are `cloud02`+
- The self-service endpoint is `/api/v3/assignments/self` (NOT `/api/v3/assignments/`)
- Basic auth (`-u email:password`) works for GET/read; write operations need Bearer token
- Host interface for 100G traffic: `em3` (Mellanox, 100 Gbps) on R6625 hosts
- Default SSH key for reserved hosts: from `ssh_key_path` in quads config
- QUADS self-service doc: https://github.com/quadsproject/quads/blob/latest/docs/quads-self-schedule.md
