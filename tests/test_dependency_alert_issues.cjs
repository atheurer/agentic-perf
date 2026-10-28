const assert = require("node:assert/strict");
const test = require("node:test");
const { syncAlerts } = require("../scripts/dependency_alert_issues.cjs");

function alert(manifest, version = "= 1.0") {
  return {
    dependency: { package: { name: "Example_Pkg", ecosystem: "pip" },
      manifest_path: manifest, version_requirement: version },
    security_advisory: { ghsa_id: "GHSA-aaaa-bbbb-cccc", cve_id: "CVE-2026-1234",
      html_url: "https://github.com/advisories/GHSA-aaaa-bbbb-cccc" },
    security_vulnerability: { first_patched_version: { identifier: "1.1" } },
    html_url: "https://github.com/example/repo/security/dependabot/1",
  };
}

function mockGitHub(alerts, issues = []) {
  const calls = [];
  const listAlertsForRepo = Symbol("alerts");
  const listForRepo = Symbol("issues");
  return {
    calls,
    rest: { dependabot: { listAlertsForRepo }, issues: { listForRepo,
      create: async (args) => calls.push({ action: "create", ...args }),
      update: async (args) => calls.push({ action: "update", ...args }),
    } },
    paginate: async (method) => method === listAlertsForRepo ? alerts : issues,
  };
}

test("groups the same advisory across locks and ignores unrelated manifests", async () => {
  const github = mockGitHub([alert("requirements.lock"), alert("requirements-dev.lock"),
    alert("pyproject.toml")]);
  await syncAlerts(github, "owner", "repo");
  assert.equal(github.calls.length, 1);
  assert.equal(github.calls[0].action, "create");
  assert.match(github.calls[0].body, /requirements\.lock/);
  assert.match(github.calls[0].body, /requirements-dev\.lock/);
  assert.doesNotMatch(github.calls[0].body, /pyproject\.toml/);
  assert.match(github.calls[0].title, /CVE-2026-1234/);
});

test("does not duplicate an existing matching issue and reopens it if needed", async () => {
  const initial = mockGitHub([alert("requirements.lock")]);
  await syncAlerts(initial, "owner", "repo");
  const issue = { number: 42, state: "open", ...initial.calls[0] };
  const unchanged = mockGitHub([alert("requirements.lock")], [issue]);
  await syncAlerts(unchanged, "owner", "repo");
  assert.equal(unchanged.calls.length, 0);
  const closed = mockGitHub([alert("requirements.lock")], [{ ...issue, state: "closed" }]);
  await syncAlerts(closed, "owner", "repo");
  assert.equal(closed.calls[0].action, "update");
  assert.equal(closed.calls[0].state, "open");
});

test("does not create an issue without a lock-file alert", async () => {
  const github = mockGitHub([alert("pyproject.toml")]);
  await syncAlerts(github, "owner", "repo");
  assert.deepEqual(github.calls, []);
});

test("does not duplicate a manually filed issue for the same package and CVE", async () => {
  const github = mockGitHub([alert("requirements.lock")], [{
    number: 22, state: "open", title: "fix(deps): Example-Pkg CVE-2026-1234",
    body: "Investigating",
  }]);
  await syncAlerts(github, "owner", "repo");
  assert.deepEqual(github.calls, []);
});

test("reports an unavailable fix instead of assuming one exists", async () => {
  const vulnerable = alert("requirements.lock");
  vulnerable.security_vulnerability.first_patched_version = null;
  const github = mockGitHub([vulnerable]);
  await syncAlerts(github, "owner", "repo");
  assert.match(github.calls[0].body, /first fixed version: not available/);
});
