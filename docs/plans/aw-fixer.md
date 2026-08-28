# `aw-fixer` agentic workflow plan

## Goal

Add an agentic workflow named `aw-fixer` that continuously reviews XHarness
agentic-workflow runs and agentic-workflow-generated issues, deduplicates related
signals, and opens a draft pull request for each actionable problem.

Every created pull request has a title beginning with `[aw-fixer] ` and contains
no file changes. It serves as a complete diagnosis and handoff for a human or a
later Copilot session. Automatic code changes are deferred until an
organization-managed GitHub App is available for workflow-file writes.

## Findings that shape the design

- GitHub Agentic Workflows supports `workflow_run`, but that trigger requires a
  static, non-empty workflow-name list. A scheduled inventory is therefore the
  only robust way to include newly added workflows without updating
  `aw-fixer`.
- Discovery must use both the current default-branch workflow files and the
  Actions API rather than only globbing the agent checkout. Historical
  workflows removed from the default branch are out of scope, even if old runs
  remain visible through the Actions API.
- The repository's generated agentic-workflow issues use the existing
  `agentic-workflows` label (plural), not `agentic-workflow`.
- Generated failure issues contain stable metadata and run URLs in their body.
  Later occurrences can be appended as comments, as seen in issue #1699.
- `create-pull-request` supports `allow-empty`, which creates an empty commit and
  allows a real pull request with zero changed files.
- Safe-output temporary IDs allow a newly requested pull request to be targeted
  by `add-comment` operations in the same run.
- Safe-output `data` is preserved by the validator and appended to item bodies
  as fenced JSON. Use this mechanism for aw-fixer fingerprints; do not rely on
  agent-authored HTML comments surviving safe-output processing.
- Changes under `.github/workflows/` require `allow-workflows: true`, a GitHub
  App safe-output credential with workflow-write capability, and an explicit
  protected-file policy. The normal `GITHUB_TOKEN` is not sufficient. V1 avoids
  this dependency by creating empty handoff PRs and making no repository edits.
- The generated lock file for Runtime Failure Observer records gh-aw `v0.86.2`.
  Compile the new workflow with that version unless an upgrade is intentional.
- gh-aw `on.steps` can run deterministic GitHub API collection in the
  pre-activation job, expose explicit job outputs, and gate the agent with
  `needs.pre_activation.outputs`. This avoids scheduling an agent job when the
  collector finds no candidate.

## Trigger and inventory model

Run every 12 hours and scan the previous 24 hours. Also support
`workflow_dispatch` with a bounded `lookback_hours` override of up to 168 hours
for recovery scans, and give the workflow a non-cancelling `aw-fixer`
concurrency group.

A deterministic pre-activation inventory step will:

1. Enumerate current `.agent.md` sources and corresponding `.agent.lock.yml`
   workflows from the default branch.
2. Query every completed run for those workflows in the 24-hour lookback
   window.
3. Ignore Actions history for workflows no longer present on the default branch.
4. Record every completed run as reviewed.
5. Forward only `failure`, `timed_out`, `cancelled`, and `action_required`
   conclusions to the agent. Expected manual cancellation or approval state can
   later be classified as non-actionable.
6. Query open issues authored by `github-actions` (the REST API login is
   `github-actions[bot]`), with a title beginning `[aw]` and the
   `agentic-workflows` label.
7. Consider every qualifying open issue regardless of age. For ordinary issues,
   treat the issue and its run-linked comments as one signal. For Detection Runs,
   enumerate comment-level occurrences and remove comments already handled by
   exact aw-fixer metadata. This lets the first deployment drain the existing
   backlog while later runs continue to discover new comments.
8. Search open and recently merged PRs for exact source URLs and fenced
   aw-fixer metadata so already-handled occurrences can be removed without
   model judgment. Keep occurrences that still require a missing PR-body link
   or issue backlink.
9. Skip the agent job entirely when no potential signal exists.

Implement inventory and evidence collection as checked-in deterministic code
invoked by `actions/github-script` from `on.steps`, after a read-only checkout.
The code gathers workflow/run IDs, conclusions, URLs, failed jobs and steps,
generated issue metadata, raw log locations, and exact deduplication evidence.
It exposes:

- `has_candidates`, used by the top-level `if` expression to gate the agent;
- a compact `candidate_manifest` string passed into the prompt through
  `needs.pre_activation.outputs`; and
- a scan summary listing reviewed run IDs and counts in the pre-activation step
  summary.

Bound the manifest to the 50 oldest potential signals and include identifiers,
URLs, and short metadata only, not issue bodies or logs. The agent retrieves the
full evidence through GitHub MCP and adds only the failure classification,
normalized summary, selected log excerpts, semantic matching, and root-cause
reasoning that require model judgment.

Grant the pre-activation job only `actions: read`, `contents: read`,
`issues: read`, and `pull-requests: read`.

Use the overlapping 24-hour window rather than persistent mutable state.
Fingerprint metadata and deduplication make rescanning safe, while the overlap
avoids gaps after a delayed or failed scheduled run.

This inventory naturally includes `aw-fixer` after its first run. A failed
`aw-fixer` run is analyzed by the next successful scheduled or manually
dispatched run. A workflow that cannot reach its agent job on any subsequent run
cannot repair itself; the generated `[aw]` issue and manual dispatch remain the
bootstrap path.

## Canonical incident model

Treat runs, generated issues, and generated issue comments as evidence, not
automatically as separate problems.

For each signal, extract:

- workflow ID, workflow path, and workflow name;
- run ID and run URL;
- conclusion and failure category;
- failing job and step;
- job/log URL and a short exact failure excerpt;
- generated issue number and exact comment permalink, when present.

Build two identifiers:

1. **Occurrence key**: the exact run ID, or the issue-comment ID when no run ID
   can be extracted.
2. **Incident fingerprint**: workflow ID + failure category + normalized failing
   job/step + normalized primary error.

The occurrence key prevents processing the same report twice. The incident
fingerprint groups repeated runs and the issue that reported them into one
actionable problem.

Include both in the safe-output `data` object for every PR and aw-fixer comment.
Use a `kind` value so the analysis, next-action, and backlink comments can be
checked independently:

```json
{
  "aw_fixer": {
    "schema": 1,
    "kind": "handoff-pr|analysis|next-action|backlink",
    "occurrence_keys": ["run:<id>"],
    "incident_fingerprint": "<fingerprint>",
    "source_runs": ["<url>"],
    "source_issues": ["<issue-or-comment-url>"]
  }
}
```

The safe-output processor appends this as fenced JSON to the resulting item,
making the fingerprint durable and searchable without depending on custom HTML
comments.

## Decision process

Process incidents oldest first so a noisy recent failure does not starve an
older one.

### 1. Confirm the evidence

Fetch the run, failing jobs, relevant failed-step logs, generated issue, and
relevant comments through GitHub MCP. Use the Actions toolset for exact evidence
and stable job URLs.

If the run or issue lacks enough evidence to identify the failed workflow stage,
the incident is still actionable as a handoff, but no fix is attempted.

### 2. Unify run and issue reports

If a generated issue body or comment contains the same run URL, merge those
signals before searching for existing work. This is the primary guard against
opening one PR from the run and another from the issue.

### 3. Search for existing work

Give the agent GitHub MCP access for all GitHub reads, including the `actions`,
`repos`, `pull_requests`, `issues`, and `search` toolsets. Do not expose `curl`
or general-purpose shell access. Deterministic pre-activation collection uses
the official GitHub API client.

When an issue search requests only integrity-approved items, always request the
`user` field as well. This avoids the known search/integrity failure for items
created by agentic bots.

Search in this order:

1. Open PR containing the exact occurrence key, incident fingerprint, run URL,
   issue URL, or issue-comment permalink.
2. PR merged in the last three days with the same exact references.
3. Open PR whose title, body, changed workflow/support files, and failure
   signature collectively indicate the same root cause.
4. PR merged in the last three days with the same semantic evidence and merged
   after the reported occurrence.
5. Existing generated or human-filed issue describing the same incident.

Do not treat title similarity alone as proof that a PR fixes the incident.

Outcomes:

- **Matching open PR:** do not create another PR. Add any missing generated
  issue link to the PR's aw-fixer-managed body island. Ensure the generated issue
  has one aw-fixer comment linking to the PR.
- **Matching recently merged PR:** do not create another PR. If a generated
  issue exists, ensure it has one aw-fixer comment linking to the merged PR.
  Only add a missing issue link to the merged PR body when the match is strong.
  A false semantic match can suppress action for at most three days; a
  persistent failure becomes eligible for a new fixer PR after that window.
- **Matching issue but no PR:** continue and create a PR. Start the PR body by
  stating that it exists to address the issue and link the issue.
- **No matching work:** continue and create a PR from the run evidence.

Use `update-pull-request` with `target: "*"` and `operation:
replace-island`, with title updates disabled. This confines edits to an
aw-fixer-owned section instead of replacing a human-authored PR description.

### 4. Prepare the recommended fix or handoff

Read the failing workflow source, its generated lock file, imported shared
files, and relevant helper/tests. Check recent changes to avoid proposing a fix
already present on `main`.

Classify the next step as one of:

- **Small recommended fix:** a complete change of at most 50 authored changed
  lines across at most three files. Exclude the corresponding generated
  `.agent.lock.yml` diff from that estimate. Describe the exact files, edits,
  and validation commands, but do not apply them.
- **Needs information or decision:** state the missing input and the decision
  options precisely.
- **Larger fix:** explain why the complete change exceeds the small-fix bounds
  and identify a safe implementation sequence.
- **No repository fix:** explain why the failure is external, expected, or no
  longer present on `main`.

### 5. Create the draft PR and comments

Request one draft PR with:

- title prefix `[aw-fixer] `;
- `agentic-workflows` label;
- a branch name derived from the workflow ID and incident fingerprint;
- `auto-close-issue: false`, because generated tracking issues can aggregate
  multiple occurrences and must not be closed implicitly;
- `allow-empty: true`, so every v1 handoff PR has an empty commit and zero
  changed files;
- a temporary safe-output ID used by same-run comments.

The PR body will include:

1. A first sentence linking the generated issue, when one exists.
2. The exact run, job/log, and issue-comment links.
3. The exact failure excerpt.
4. The incident fingerprint and dedup evidence.
5. The recommended fix or the exact limitation.
6. Enough source locations and next-step context for another Copilot session to
   continue without repeating discovery.

Always add an **analysis comment** to the new PR through its temporary ID. It
contains the detailed evidence, likely root cause, relevant source locations,
searches performed, and rejected alternatives.

Always add a second **next-action comment**. It contains the proposed small
change and validation commands, the missing decision or information, the larger
implementation sequence, or the reason no repository fix is appropriate. It
also states that v1 intentionally made no repository edits. The safe-output
`data` fingerprint makes both comments idempotent.

### 6. Link generated issues back to the PR

For each generated issue represented by the incident, add one comment linking
to the matching open, merged, or newly created PR unless its fenced aw-fixer
metadata shows that link already exists.

Describe the link as **"PR with a candidate fix"**. Never call it a "draft PR"
in an issue comment because draft status is temporary.

Do not close generated issues and do not use closing keywords in these links.

## Special handling for `[aw] Detection Runs`

The current example is #1697. The official gh-aw template identifies this
class of issue with
`<!-- gh-aw-detection-runs -->`, says it is automatically managed, and records
each detection warning or failure as a separate comment. It is a stream of
occurrences, not one fixable incident.

Handle it as follows:

1. Never close, assign, retitle, or rewrite the issue.
2. Treat each comment permalink and its run URL as an occurrence.
3. Use the normal incident fingerprint to combine comments that represent the
   same root cause.
4. Discover the current issue by author, title, label, and the framework marker;
   never hardcode #1697.
5. Link a PR to both the discovered parent issue and the exact detection comment
   permalink.
6. Add one backlink comment to the discovered issue per matching PR. The comment
   names it as a **"PR with a candidate fix"**, never a "draft PR", and lists
   the specific detection comment(s) it covers.
7. Use `Related to #<discovered-number>`, never a closing keyword.

This explicitly supports many PRs linked to the current Detection Runs issue.
The parent remains the framework-managed index, while each aw-fixer backlink
comment documents the many-to-one relationship and points to the exact
comment-level occurrence.

## Safe-output and permission shape

The workflow will use read-only agent permissions and perform every write
through safe outputs:

- `create-pull-request`: draft, `[aw-fixer] ` prefix, `allow-empty: true`, and
  no workflow-file write capability, maximum two;
- `update-pull-request`: body-only, wildcard target, replace-island operation,
  maximum two;
- `add-comment`: wildcard target, maximum eight for the two PR comments and
  issue backlinks associated with the two processed incidents;
- `noop`: no issue report;
- missing-tool/missing-data outputs without creating additional tracking issues.

The workflow will reuse the repository PAT-pool import for Copilot inference.
Do not configure `safe-outputs.github-app`, `allow-workflows`, code-writing
tools, or a file allowlist in v1.

## Safety and bounded operation

- Process at most two new incidents and create at most two PRs per run; later
  runs drain the backlog.
- Never merge or mark PRs ready for review.
- Never update arbitrary PR text outside the aw-fixer-managed island.
- Never alter generated issue state.
- Never edit repository files in v1.
- Do not expose `curl` or generic bash/shell tools to the agent.
- Sanitize log excerpts and avoid copying credentials or environment dumps.
- Use exact run/job/comment URLs rather than reconstructed links.
- Stop with a visible workflow failure if required GitHub reads fail; do not
  emit success-shaped output for an incomplete scan.

## Implementation outline

1. Add a small deterministic candidate-collection module under
   `.github/workflows/`. In `on.steps`, check out the default branch read-only
   and invoke the module from `actions/github-script`. Expose
   `has_candidates` and `candidate_manifest` through
   `jobs.pre-activation.outputs`, and gate execution with
   `if: needs.pre_activation.outputs.has_candidates == 'true'`.
2. Add `.github/workflows/aw-fixer.agent.md` with the schedule, PAT-pool import,
   read tools, safe outputs, decision instructions, and the candidate manifest
   embedded in its prompt.
3. Compile `.github/workflows/aw-fixer.agent.lock.yml` with gh-aw `v0.86.2`
   unless the repository intentionally upgrades the compiler first.
4. Add fixture-based tests with Node's built-in `node:test` runner. Keep API
   retrieval behind a small adapter and test only deterministic inventory,
   parsing, ordering, and exact-deduplication functions with checked-in JSON
   fixtures covering:
    - dynamic discovery of all recent `.agent.lock.yml` runs;
    - exclusion of workflows removed from the default branch;
    - failure-conclusion filtering and oldest-first ordering;
    - discovery of old, still-open generated issues during the initial backlog
      drain;
    - run/issue/comment unification;
    - parsing safe-output fenced data and exact PR deduplication by source URLs;
    - normalization of the `github-actions[bot]` API login;
    - dynamic Detection Runs comment-level handling;
    - per-run incident and PR caps;
    - self-discovery of an `aw-fixer` run.
5. Compile in strict mode and run the deterministic helper tests. Do not add
   automated assertions for the LLM's semantic matching, diagnosis, log-excerpt
   selection, or fix recommendation in v1.

## Success criteria

- Every completed agentic-workflow run in the lookback window is inventoried.
- Every qualifying open generated issue is inventoried regardless of age.
- Only failure-like conclusions and generated issue signals reach deep analysis.
- A run and its generated issue produce at most one incident and one PR.
- Existing open or recently merged fixes suppress duplicate PR creation.
- Existing generated issues are linked in both directions with matching PRs.
- New actionable incidents create a draft `[aw-fixer]` PR with exact evidence.
- Every created PR has zero changed files and complete analysis and next-action
  comments.
- When a small fix is apparent, the handoff identifies the exact files, edits,
  and validation commands without applying the change.
- Detection Runs comments can map to separate PRs without closing or overloading
  the current framework-managed parent issue.
- A later successful `aw-fixer` run can analyze a prior failed `aw-fixer` run.

## References

- [Trigger events](https://github.github.com/gh-aw/reference/triggers/)
- [Safe outputs](https://github.github.com/gh-aw/reference/safe-outputs/)
- [Pull-request safe outputs](https://github.github.com/gh-aw/reference/safe-outputs-pull-requests/)
- [Monitoring and audit commands](https://github.github.com/gh-aw/experimental/monitoring-with-projects/)
- [Detection Runs issue #1697](https://github.com/dotnet/xharness/issues/1697)
- [Generated failure issue #1699](https://github.com/dotnet/xharness/issues/1699)
