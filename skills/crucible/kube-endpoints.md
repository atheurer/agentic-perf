# Kube endpoint workflow

Use a kube endpoint only when the ticket's assigned infrastructure and
Crucible controller context establish the Kubernetes host and access path. Do
not assume that the controller is also the Kubernetes host, that targets are
empty, or that a particular kubeconfig path is present.

Build the endpoint from the current controller-sourced run-file schema and
examples returned by get_skill_context. Use the assigned host identity and
verified SSH address; do not infer either from benchmark traffic addresses.
Validate the complete run file with validate_benchmark before presenting it
for approval or executing it. If the schema, assignment, or required access
details disagree, ask for clarification rather than constructing a guessed
endpoint.
