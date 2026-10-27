// Synchronize open Dependabot alerts for the Python lock inventory to issues.
const LOCKS = new Set(["requirements.lock", "requirements-dev.lock"]);

function alertKey(alert) {
  const name = alert.dependency.package.name.toLowerCase().replace(/[-_.]+/g, "-");
  return `${name}:${alert.security_advisory.ghsa_id}`;
}

function issueContent(key, alerts) {
  const first = alerts[0];
  const name = first.dependency.package.name;
  const advisory = first.security_advisory;
  const marker = `<!-- dependency-alert:${key} -->`;
  const lines = alerts
    .map((alert) => {
      const dependency = alert.dependency;
      const fix = alert.security_vulnerability.first_patched_version?.identifier;
      return `- ${dependency.manifest_path}: ${dependency.version_requirement}; ` +
        `first fixed version: ${fix || "not available"} ([alert](${alert.html_url}))`;
    })
    .sort();
  const identifiers = [advisory.ghsa_id, advisory.cve_id].filter(Boolean).join(" / ");
  const title = `Security: ${name} ${identifiers}`;
  const body = [
    marker,
    `GitHub reports a vulnerability in **${name}** in the Python lock inventory.`,
    "",
    `Advisory: [${identifiers}](https://github.com/advisories/${advisory.ghsa_id})`,
    "",
    ...lines,
    "",
    "Update both affected lock files and any necessary project constraints; " +
      "verify the fix and CI before merging. If no compatible fix exists, " +
      "investigate mitigations and track the blocker here.",
  ].join("\n");
  return { marker, title, body };
}

async function syncAlerts(github, owner, repo) {
  const alerts = await github.paginate(github.rest.dependabot.listAlertsForRepo, {
    owner, repo, state: "open", per_page: 100,
  });
  const grouped = new Map();
  for (const alert of alerts) {
    if (alert.dependency.package.ecosystem !== "pip" ||
        !LOCKS.has(alert.dependency.manifest_path)) continue;
    const key = alertKey(alert);
    grouped.set(key, [...(grouped.get(key) || []), alert]);
  }
  if (!grouped.size) return;

  const issues = await github.paginate(github.rest.issues.listForRepo, {
    owner, repo, state: "all", per_page: 100,
  });
  for (const [key, matches] of grouped) {
    const { marker, title, body } = issueContent(key, matches);
    const existing = issues.find((issue) => !issue.pull_request &&
      (issue.body || "").includes(marker));
    const identifiers = [matches[0].security_advisory.ghsa_id,
      matches[0].security_advisory.cve_id].filter(Boolean);
    const manualIssue = issues.find((issue) => !issue.pull_request && issue.state === "open" &&
      identifiers.some((id) => issue.title.includes(id)) &&
      issue.title.toLowerCase().replace(/[-_.]+/g, "-").includes(key.split(":")[0]));
    if (!existing) {
      if (!manualIssue) await github.rest.issues.create({ owner, repo, title, body });
    } else if (existing.state === "closed") {
      await github.rest.issues.update({ owner, repo, issue_number: existing.number, state: "open" });
    }
  }
}

module.exports = { alertKey, issueContent, syncAlerts };
