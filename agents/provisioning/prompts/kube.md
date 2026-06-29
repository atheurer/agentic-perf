## Kubernetes Endpoint Setup

If the ticket's directives include `endpoint_type: kube`:

First, determine whether the controller already has access to a
Kubernetes/OpenShift cluster. Look for clues in this order:

a. **Ticket context** — if the user mentioned an existing cluster
   (e.g., "my OpenShift cluster", "cluster sno-3c", a cluster API URL),
   or if the harness targets external clusters (benchmark-runner always
   does), then an existing cluster is expected. Do NOT install K3s.

b. **Detect on the host** — check if a working kubeconfig exists:
   run `kubectl cluster-info` or `oc cluster-info` on the controller.
   If a live cluster is detected, skip K8s installation and report
   what was found (cluster API URL, version, node count).

c. **Install K8s** — only if no existing cluster is detected AND the
   ticket does not reference an existing cluster. Use the install_k3s
   tool (the default K8s distribution). The installer handles
   kubeconfig setup, kubectl availability, and self-SSH.

d. **Ask the user** — if the situation is ambiguous (e.g., a stale
   kubeconfig exists but the cluster is unreachable), use
   request_clarification to ask whether to install a new cluster
   or fix the existing one.
