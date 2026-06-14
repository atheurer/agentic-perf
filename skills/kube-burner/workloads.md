# kube-burner Workloads

kube-burner stress-tests Kubernetes clusters by creating and
deleting objects at scale. It connects directly via kubeconfig
(no SSH to kube host needed).

## Available Workloads

### node-density

Fills a node with pause pods to measure pod startup latency.

- **What it creates**: Pods (one per iteration) using the
  `pause:3.9` container image
- **Key metric**: Pod startup latency (scheduling, initialized,
  containersReady, ready)
- **Typical use**: "How many pods can this node handle?"
- **Default**: 50 pods, qps=20, burst=20

### cluster-density

Stresses the K8s API and etcd by creating multiple resource
types across namespaces.

- **What it creates per namespace**: 1 Deployment (with 1 pod),
  1 Service, 1 ConfigMap, 1 Secret
- **Key metric**: API latency, etcd write throughput, pod
  startup latency
- **Typical use**: "How fast can the control plane handle
  object creation at scale?"
- **Default**: 10 namespaces, qps=20, burst=20

## Key Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| jobIterations | int | 50/10 | Number of iterations (pods or namespaces) |
| qps | int | 20 | API rate limit (queries per second) |
| burst | int | 20 | API burst limit |
| gc | bool | true | Delete created resources after run |
| timeout | string | "30m" | Global timeout |
| podWait | bool | true | Wait for pods to reach Running state |

## Output

kube-burner prints pod latency summaries to stdout:

```
Pod latencies:
            50th: 1234ms
            99th: 5678ms
           Ready: 2345ms
```

Local indexing is the default — JSON metrics files are written
to `collected-metrics/` in the working directory automatically
when no Elasticsearch endpoint is configured.

## Exit Codes

- 0: Success
- 1: Error
- 2: Timeout (global timeout exceeded)
- 3: Alerting error (alert thresholds exceeded)
