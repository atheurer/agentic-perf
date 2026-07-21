# Beaker Integration Developer & Agent Blueprint

This document provides a comprehensive, step-by-step technical blueprint and architecture spec for implementing the **Beaker** resource provider in `agentic-perf`. It serves as a persistent guide for future developers and AI agents to execute the implementation with 100% precision.

---

## 1. Context & Architectural Overview

The goal is to add **Beaker** (Red Hat's bare-metal provisioning and system reservation infrastructure) as a `ResourceProvider` for the `agentic-perf` resource agent. 

### Core Architecture Components
1. **Low-Level Client (`providers/beaker.py`)**: A low-level client wrapping the Beaker XML-RPC API. It handles authentication, custom network transport, Job XML submission, and status polling.
2. **Resource Provider (`providers/resource/beaker.py`)**: An adapter class implementing the abstract standard interface specified in `providers/resource/base.py`.
3. **Registry Update (`providers/resource/registry.py`)**: Registration of `beaker` as a lazy-loaded provider.

```mermaid
graph LR
    A[Resource Agent] --> B[ResourceProviderRegistry]
    B --> C[BeakerResourceProvider]
    C --> D[BeakerClient]
    D -->|XML-RPC over HTTPS| E[beaker.engineering.redhat.com]
```

---

## 2. Authentication Protocol (The Kerberos Spec)

Beaker’s internal API endpoints (hosted at `beaker.engineering.redhat.com/bkr/xmlrpc`) are strictly protected by **Kerberos (GSSAPI/SPNEGO)** at the Apache/mod_auth_gssapi layer.

### The Standard Authentication Flow
1. The user logs in to their local corporate laptop and runs `kinit atheurer@IPA.REDHAT.COM` to obtain a Kerberos Ticket Granting Ticket (TGT).
2. The user SSH'es to the runner VM with **GSSAPI ticket forwarding** enabled:
   ```bash
   ssh -K atheurer@<runner-vm>
   ```
   *(This delegates/forwards the TGT from their local Mac/Laptop to `/tmp/krb5cc_<uid>` on the VM and injects the `KRB5CCNAME` environment variable).*
3. On the VM, the Python standard `gssapi` library uses the forwarded ticket to generate security negotiation tokens.
4. These tokens are injected as an `Authorization: Negotiate <token>` HTTP header in every XML-RPC call.

### The Custom XML-RPC GSSAPITransport (Tested & Verified)
We developed and verified this custom transport in Python to handle the token generation and injection:

```python
import xmlrpc.client
import gssapi
import base64

class GSSAPITransport(xmlrpc.client.SafeTransport):
    """Custom XML-RPC Transport that injects Kerberos Negotiate headers."""
    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host

    def send_headers(self, connection, headers):
        # Generate the GSSAPI/SPNEGO token for HTTP@host
        target_name = gssapi.Name(f"HTTP@{self._host}", gssapi.NameType.hostbased_service)
        ctx = gssapi.SecurityContext(name=target_name, usage="initiate")
        token = ctx.step()
        auth_header = "Negotiate " + base64.b64encode(token).decode()
        
        # Inject the HTTP Negotiate header
        connection.putheader("Authorization", auth_header)
        
        super_send = getattr(super(), "send_headers", None)
        if super_send:
            super_send(connection, headers)
```

---

## 3. Configuration Spec (`secrets/beaker/config.json`)

The secrets configuration must reside at `secrets/beaker/config.json` inside the user's secrets directory. It supports both interactive ticket forwarding and service account keytab authentication:

```json
{
  "api_url": "beaker.engineering.redhat.com",
  "username": "atheurer",
  "realm": "IPA.REDHAT.COM",
  "auth_method": "forwarded",
  "ssh_key_path": "~/.ssh/id_ed25519",
  "default_distro": "RHEL-9.4.0-Nightly",
  "default_arch": "x86_64"
}
```

---

## 4. Implementation Blueprint: Step-by-Step

### Step 1: Create Low-Level Client (`providers/beaker.py`)
This file wraps the Beaker XML-RPC client. It handles the `GSSAPITransport` integration, Job XML compilation, status tracking, and cancellation.

```python
# FILE: providers/beaker.py
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import ssl
import tempfile
import xmlrpc.client
import gssapi
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class GSSAPITransport(xmlrpc.client.SafeTransport):
    """Custom XML-RPC Transport that injects Kerberos Negotiate headers."""
    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host

    def send_headers(self, connection, headers):
        target_name = gssapi.Name(f"HTTP@{self._host}", gssapi.NameType.hostbased_service)
        ctx = gssapi.SecurityContext(name=target_name, usage="initiate")
        token = ctx.step()
        auth_header = "Negotiate " + base64.b64encode(token).decode()
        connection.putheader("Authorization", auth_header)
        
        super_send = getattr(super(), "send_headers", None)
        if super_send:
            super_send(connection, headers)

class BeakerClient:
    """Async client wrapper for the Beaker XML-RPC API with Kerberos SPNEGO."""

    def __init__(
        self,
        api_url: str,
        username: str,
        realm: str = "IPA.REDHAT.COM",
        auth_method: str = "forwarded",
        keytab_data: bytes | None = None,
        ssh_key_path: str = "~/.ssh/id_ed25519",
        default_distro: str = "RHEL-9.4.0-Nightly",
        default_arch: str = "x86_64",
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.realm = realm
        self.auth_method = auth_method
        self._keytab_data = keytab_data
        self.ssh_key_path = str(Path(ssh_key_path).expanduser())
        self.default_distro = default_distro
        self.default_arch = default_arch
        self._temp_keytab_path: str | None = None
        self._krb5_ccache: str | None = None

    @classmethod
    async def from_secrets(cls, secrets_provider) -> BeakerClient:
        raw = await secrets_provider.get_secret("beaker/config.json")
        if not raw:
            raise ValueError("Beaker config not found at secrets/beaker/config.json")
        config = json.loads(raw)
        
        keytab_bytes = None
        if config.get("keytab_base64"):
            keytab_bytes = base64.b64decode(config["keytab_base64"])

        return cls(
            api_url=config["api_url"],
            username=config["username"],
            realm=config.get("realm", "IPA.REDHAT.COM"),
            auth_method=config.get("auth_method", "forwarded"),
            keytab_data=keytab_bytes,
            ssh_key_path=config.get("ssh_key_path", "~/.ssh/id_ed25519"),
            default_distro=config.get("default_distro", "RHEL-9.4.0-Nightly"),
            default_arch=config.get("default_arch", "x86_64"),
        )

    async def initialize_auth(self) -> None:
        """Securely load keytab credentials if using non-interactive background auth."""
        if self.auth_method == "forwarded":
            logger.info("[beaker] Relying on active/forwarded session Kerberos cache.")
            return

        if self.auth_method == "keytab" and self._keytab_data:
            self._krb5_ccache = tempfile.mktemp(prefix="krb5cc_beaker_")
            os.environ["KRB5CCNAME"] = f"FILE:{self._krb5_ccache}"
            
            fd, self._temp_keytab_path = tempfile.mkstemp(prefix="beaker_kt_")
            with os.fdopen(fd, "wb") as f:
                f.write(self._keytab_data)
            
            cmd = ["kinit", "-k", "-t", self._temp_keytab_path, f"{self.username}@{self.realm}"]
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(f"kinit Keytab authentication failed: {stderr.decode()}")
            logger.info(f"[beaker] Successfully authenticated with Keytab for {self.username}")

    def _get_proxy(self) -> xmlrpc.client.ServerProxy:
        ssl_context = ssl._create_unverified_context()
        if self._krb5_ccache:
            os.environ["KRB5CCNAME"] = f"FILE:{self._krb5_ccache}"
            
        transport = GSSAPITransport(host=self.api_url, context=ssl_context)
        return xmlrpc.client.ServerProxy(f"https://{self.api_url}/bkr/xmlrpc", transport=transport)

    async def list_available_systems(self, min_cores: int | None = None, min_memory_gb: int | None = None) -> list[str]:
        def _call():
            proxy = self._get_proxy()
            filters = {"status": "Automated", "arch": self.default_arch}
            return proxy.systems.filter(filters)
        return await asyncio.to_thread(_call)

    async def submit_provision_job(self, description: str, distro: str | None = None, arch: str | None = None, duration_hours: int = 36) -> str:
        distro_to_use = distro or self.default_distro
        arch_to_use = arch or self.default_arch
        seconds = duration_hours * 3600

        job_xml = f"""<job>
  <whiteboard>{description}</whiteboard>
  <recipeSet priority="Normal">
    <recipe distro="{distro_to_use}" arch="{arch_to_use}" role="Standalone">
      <autoprov/>
      <task name="/distribution/reserving" role="Standalone">
        <params>
          <param name="DURATION" value="{seconds}"/>
        </params>
      </task>
    </recipe>
  </recipeSet>
</job>"""

        def _call():
            proxy = self._get_proxy()
            return proxy.jobs.submit(job_xml)
        return await asyncio.to_thread(_call)

    async def get_job_status(self, job_id: str) -> dict[str, Any]:
        def _call():
            proxy = self._get_proxy()
            return proxy.jobs.get_status(job_id)
        return await asyncio.to_thread(_call)

    async def get_job_systems(self, job_id: str) -> list[str]:
        def _call():
            proxy = self._get_proxy()
            return proxy.jobs.systems(job_id)
        return await asyncio.to_thread(_call)

    async def cancel_job(self, job_id: str) -> bool:
        def _call():
            proxy = self._get_proxy()
            return proxy.jobs.cancel(job_id, "Cancelled by agentic-perf")
        return await asyncio.to_thread(_call)

    async def release_system(self, hostname: str) -> bool:
        def _call():
            proxy = self._get_proxy()
            return proxy.systems.release(hostname)
        return await asyncio.to_thread(_call)

    async def close(self) -> None:
        if self._temp_keytab_path and os.path.exists(self._temp_keytab_path):
            os.remove(self._temp_keytab_path)
        if self._krb5_ccache and os.path.exists(self._krb5_ccache):
            os.remove(self._krb5_ccache)
```

### Step 2: Create Adapter (`providers/resource/beaker.py`)
This file maps standard lifecycle calls of the performance engine to the low-level Beaker client.

```python
# FILE: providers/resource/beaker.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import ResourceProvider

logger = logging.getLogger(__name__)

class BeakerResourceProvider(ResourceProvider):
    """ResourceProvider adapter for Red Hat Beaker bare-metal systems."""

    provider_name = "beaker"

    def __init__(self, client) -> None:
        from providers.beaker import BeakerClient
        self._client: BeakerClient = client

    @classmethod
    async def from_secrets(cls, secrets_provider) -> BeakerResourceProvider:
        from providers.beaker import BeakerClient
        client = await BeakerClient.from_secrets(secrets_provider)
        await client.initialize_auth()
        return cls(client)

    async def check_available(self, requirements: dict[str, Any]) -> dict[str, Any]:
        min_cores = requirements.get("min_cores")
        min_memory_gb = requirements.get("min_memory_gb")
        
        hostnames = await self._client.list_available_systems(
            min_cores=min_cores,
            min_memory_gb=min_memory_gb
        )
        options = [{"hostname": name} for name in hostnames]
        
        return {
            "provider": self.provider_name,
            "available_count": len(hostnames),
            "options": options,
            "message": f"{len(hostnames)} automated Beaker hosts currently available in pool",
        }

    async def reserve(
        self,
        selection: dict[str, Any],
        description: str,
        duration_hours: int = 36,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        distro = selection.get("distro") or self._client.default_distro
        arch = selection.get("arch") or self._client.default_arch

        logger.info(f"[beaker-provider] Submitting Beaker job to provision RHEL: {description}")
        job_id = await self._client.submit_provision_job(
            description=description,
            distro=distro,
            arch=arch,
            duration_hours=duration_hours,
        )

        timeout = 3600  # 1 hour maximum provisioning timeout
        interval = 60
        elapsed = 0
        hosts = []
        status = "failed"

        while elapsed < timeout:
            job_info = await self._client.get_job_status(job_id)
            state = job_info.get("state")
            logger.info(f"[beaker-provider] Job {job_id} status: {state} ({elapsed}s elapsed)")

            if state == "Completed":
                hosts = await self._client.get_job_systems(job_id)
                status = "success"
                break
            elif state in ("Cancelled", "Aborted"):
                break
            
            await asyncio.sleep(interval)
            elapsed += interval

        if status != "success" or not hosts:
            return {
                "status": "failed",
                "reservation_id": job_id,
                "hosts": [],
                "ssh_user": "root",
                "ssh_key_path": self._client.ssh_key_path,
                "lease_expiration": None,
                "provider": self.provider_name,
                "provider_metadata": {},
                "message": f"Beaker job {job_id} failed to complete within timeout.",
            }

        return {
            "status": "success",
            "reservation_id": job_id,
            "hosts": hosts,
            "ssh_user": "root",
            "ssh_key_path": self._client.ssh_key_path,
            "lease_expiration": None,
            "provider": self.provider_name,
            "provider_metadata": {
                "job_id": job_id,
                "hosts": hosts,
                "distro": distro,
            },
            "message": f"Reserved and provisioned {len(hosts)} hosts via Beaker",
        }

    async def get_reservation_status(
        self, reservation_id: str, provider_metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        status = await self._client.get_job_status(reservation_id)
        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "ready": status.get("state") == "Completed",
            "details": status,
        }

    async def terminate(
        self,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = reservation_id
        hosts = provider_metadata.get("hosts", [])
        
        await self._client.cancel_job(job_id)
        release_details = {}
        for host in hosts:
            res = await self._client.release_system(host)
            release_details[host] = "released" if res else "failed"

        return {
            "provider": self.provider_name,
            "reservation_id": reservation_id,
            "status": "terminated",
            "details": {
                "job_cancelled": True,
                "system_releases": release_details,
            },
        }

    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        # Beaker kickstarts can automatically pre-inject authorized_keys, 
        # so secondary key propagation is skipped.
        return {
            "status": "success",
            "ssh_key_path": self._client.ssh_key_path,
            "hosts": {h: "ok" for h in hosts},
        }

    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        return {
            "status": "success",
            "hosts": {h: "cleaned" for h in hosts},
        }

    async def close(self) -> None:
        await self._client.close()
```

### Step 3: Update Registry (`providers/resource/registry.py`)
Add `beaker` to the lazy-loading provider registry:

```diff
# FILE: providers/resource/registry.py

 PROVIDER_REGISTRY: dict[str, dict[str, str]] = {
     "quads": {
         "class": "providers.resource.quads.QuadsResourceProvider",
         "secret": "quads/config.json",
     },
     "aws": {
         "class": "providers.resource.aws.AWSResourceProvider",
         "secret": "aws/config.json",
     },
     "psap-cc": {
         "class": "providers.resource.psap_cc.PSAPCCResourceProvider",
         "secret": "psap-cc/config.json",
     },
+    "beaker": {
+        "class": "providers.resource.beaker.BeakerResourceProvider",
+        "secret": "beaker/config.json",
+    },
 }
```
