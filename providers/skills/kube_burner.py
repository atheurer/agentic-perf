from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "density": ["node-density", "cluster-density"],
    "pod": ["node-density"],
    "node": ["node-density"],
    "scale": ["cluster-density", "node-density"],
    "stress": ["cluster-density"],
    "api": ["cluster-density"],
    "etcd": ["cluster-density"],
    "kubernetes": ["node-density", "cluster-density"],
    "k8s": ["node-density", "cluster-density"],
    "kube-burner": ["node-density", "cluster-density"],
    "kubeburner": ["node-density", "cluster-density"],
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "node-density": {
        "description": (
            "Pod density stress test — fills a node with pause pods "
            "and measures pod startup latency via the Kubernetes API"
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "jobIterations": {
                "type": "integer",
                "description": "Number of pods to create",
                "default": 50,
            },
            "qps": {
                "type": "integer",
                "description": "API queries per second rate limit",
                "default": 20,
            },
            "burst": {
                "type": "integer",
                "description": "API burst limit",
                "default": 20,
            },
            "podWait": {
                "type": "boolean",
                "description": "Wait for pods to be running before completing",
                "default": True,
            },
            "gc": {
                "type": "boolean",
                "description": "Garbage collect created namespaces after run",
                "default": True,
            },
            "timeout": {
                "type": "string",
                "description": "Global timeout (e.g., '30m', '1h')",
                "default": "30m",
            },
        },
        "default_template": {
            "pod.yml": (
                "apiVersion: v1\n"
                "kind: Pod\n"
                "metadata:\n"
                '  name: "pause-{{ .Iteration }}-{{ .Replica }}"\n'
                "  labels:\n"
                '    app: node-density\n'
                "spec:\n"
                "  containers:\n"
                '  - name: pause\n'
                '    image: registry.k8s.io/pause:3.9\n'
                "    resources:\n"
                "      requests:\n"
                '        cpu: "10m"\n'
                '        memory: "16Mi"\n'
            ),
        },
    },
    "cluster-density": {
        "description": (
            "Kubernetes API and etcd stress test — creates deployments, "
            "services, configmaps, and secrets across namespaces to "
            "measure cluster control plane throughput"
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "jobIterations": {
                "type": "integer",
                "description": "Number of namespaces to create (each gets a full set of resources)",
                "default": 10,
            },
            "qps": {
                "type": "integer",
                "description": "API queries per second rate limit",
                "default": 20,
            },
            "burst": {
                "type": "integer",
                "description": "API burst limit",
                "default": 20,
            },
            "gc": {
                "type": "boolean",
                "description": "Garbage collect created namespaces after run",
                "default": True,
            },
            "timeout": {
                "type": "string",
                "description": "Global timeout (e.g., '30m', '1h')",
                "default": "30m",
            },
        },
        "default_template": {
            "deployment.yml": (
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                '  name: "cluster-density-{{ .Iteration }}-{{ .Replica }}"\n'
                "  labels:\n"
                '    app: cluster-density\n'
                "spec:\n"
                "  replicas: 1\n"
                "  selector:\n"
                "    matchLabels:\n"
                '      app: "cluster-density-{{ .Iteration }}-{{ .Replica }}"\n'
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                '        app: "cluster-density-{{ .Iteration }}-{{ .Replica }}"\n'
                "    spec:\n"
                "      containers:\n"
                '      - name: pause\n'
                '        image: registry.k8s.io/pause:3.9\n'
                "        resources:\n"
                "          requests:\n"
                '            cpu: "10m"\n'
                '            memory: "16Mi"\n'
            ),
            "service.yml": (
                "apiVersion: v1\n"
                "kind: Service\n"
                "metadata:\n"
                '  name: "cluster-density-svc-{{ .Iteration }}-{{ .Replica }}"\n'
                "spec:\n"
                "  selector:\n"
                '    app: "cluster-density-{{ .Iteration }}-{{ .Replica }}"\n'
                "  ports:\n"
                "  - port: 80\n"
                "    targetPort: 8080\n"
            ),
            "configmap.yml": (
                "apiVersion: v1\n"
                "kind: ConfigMap\n"
                "metadata:\n"
                '  name: "cluster-density-cm-{{ .Iteration }}-{{ .Replica }}"\n'
                "data:\n"
                '  config: "generated-by-kube-burner"\n'
            ),
            "secret.yml": (
                "apiVersion: v1\n"
                "kind: Secret\n"
                "metadata:\n"
                '  name: "cluster-density-secret-{{ .Iteration }}-{{ .Replica }}"\n'
                "type: Opaque\n"
                "data:\n"
                '  key: "Z2VuZXJhdGVkLWJ5LWt1YmUtYnVybmVy"\n'
            ),
        },
    },
}


class KubeBurnerSkillProvider(SkillProvider):
    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        results = []
        for name, info in _BENCHMARKS.items():
            results.append(
                BenchmarkSuite(
                    name=name,
                    description=info["description"],
                    supported_params=info.get("params", {}),
                    endpoint_types=["kube"],
                    roles=info["roles"],
                    min_hosts=info["min_hosts"],
                    harness="kube-burner",
                )
            )
        return results

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        info = _BENCHMARKS.get(name)
        if info is None:
            return None
        return BenchmarkSuite(
            name=name,
            description=info["description"],
            supported_params=info.get("params", {}),
            endpoint_types=["kube"],
            roles=info["roles"],
            min_hosts=info["min_hosts"],
            harness="kube-burner",
        )

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        description = str(requirements.get("description", "")).lower()
        workload_type = str(requirements.get("workload_type", "")).lower()
        search_text = f"{description} {workload_type}"

        scores: dict[str, int] = {}
        for keyword, benchmarks in KEYWORD_MAP.items():
            if keyword in search_text:
                for bench in benchmarks:
                    scores[bench] = scores.get(bench, 0) + 1

        scored = {k: v for k, v in scores.items() if k in _BENCHMARKS}
        if not scored:
            return None
        return max(scored, key=scored.get)

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return RunfileTemplate(benchmark=benchmark, template={})

        defaults = {k: v["default"] for k, v in info.get("params", {}).items()}
        merged = {**defaults, **{k: v for k, v in params.items() if k in defaults}}

        job: dict[str, Any] = {
            "name": benchmark,
            "namespace": benchmark,
            "jobType": "Create",
            "jobIterations": merged.get("jobIterations", 50),
            "namespacedIterations": True,
            "cleanup": True,
            "qps": merged.get("qps", 20),
            "burst": merged.get("burst", 20),
            "objects": [],
        }

        if merged.get("podWait", False):
            job["podWait"] = True

        templates = dict(info.get("default_template", {}))
        for tpl_name in templates:
            obj = {"objectTemplate": tpl_name, "replicas": 1}
            if tpl_name == "pod.yml":
                obj["wait"] = merged.get("podWait", True)
            job["objects"].append(obj)

        config: dict[str, Any] = {
            "global": {
                "gc": merged.get("gc", True),
                "measurements": [
                    {"name": "podLatency", "thresholds": []},
                ],
            },
            "jobs": [job],
        }

        timeout = merged.get("timeout")
        if timeout:
            config["global"]["timeout"] = timeout

        template: dict[str, Any] = {
            "harness": "kube-burner",
            "config": config,
            "templates": templates,
        }

        return RunfileTemplate(benchmark=benchmark, template=template)

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None
        return info.get("params", {})

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "description": "kube-burner run-file bundle",
            "properties": {
                "harness": {"type": "string", "const": "kube-burner"},
                "config": {
                    "type": "object",
                    "description": "kube-burner config YAML content",
                    "properties": {
                        "global": {
                            "type": "object",
                            "properties": {
                                "gc": {"type": "boolean"},
                                "timeout": {"type": "string"},
                                "measurements": {"type": "array"},
                            },
                        },
                        "jobs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "jobType": {"type": "string"},
                                    "jobIterations": {"type": "integer"},
                                    "namespacedIterations": {"type": "boolean"},
                                    "qps": {"type": "integer"},
                                    "burst": {"type": "integer"},
                                    "objects": {"type": "array"},
                                },
                            },
                        },
                    },
                    "required": ["global", "jobs"],
                },
                "templates": {
                    "type": "object",
                    "description": "Object template files: {filename: yaml_content}",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["harness", "config", "templates"],
        }

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None
        result = await self.generate_runfile(benchmark, {})
        return result.template

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        errors: list[str] = []

        config = run_file.get("config")
        if not isinstance(config, dict):
            return {"valid": False, "errors": ["Missing or invalid 'config' section"]}

        if "global" not in config:
            errors.append("Missing 'global' section in config")

        jobs = config.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            errors.append("Missing or empty 'jobs' list in config")
        else:
            for i, job in enumerate(jobs):
                if not isinstance(job, dict):
                    errors.append(f"Job {i} is not a dict")
                    continue
                if "name" not in job:
                    errors.append(f"Job {i}: missing 'name' field")
                jt = job.get("jobType", "Create")
                if jt not in ("Create", "Delete", "Read", "Patch"):
                    errors.append(f"Job {i}: invalid jobType '{jt}'")
                if "objects" not in job and jt == "Create":
                    errors.append(f"Job {i}: Create job missing 'objects' list")

        templates = run_file.get("templates")
        if not isinstance(templates, dict):
            errors.append("Missing or invalid 'templates' section")
        elif isinstance(jobs, list):
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                for obj in job.get("objects", []):
                    tpl_name = obj.get("objectTemplate", "")
                    if tpl_name and tpl_name not in templates:
                        errors.append(
                            f"Object references template '{tpl_name}' "
                            f"but it is not in 'templates'"
                        )

        return {"valid": len(errors) == 0, "errors": errors}
