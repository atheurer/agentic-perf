## Kube Endpoints

For kube endpoints with managed providers, provision 1 host only — it serves
as both controller and K8s cluster node. Workloads run as pods on the cluster,
not on separate hosts. Submit assigned_hardware_ips with the controller set
and targets as an empty array [].
