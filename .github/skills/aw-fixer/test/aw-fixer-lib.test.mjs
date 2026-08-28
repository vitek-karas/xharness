import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  FAILURE_CONCLUSIONS,
  buildCandidateManifest,
  discoverCurrentWorkflows,
  extractRunReferences,
  isGeneratedAwIssue,
  normalizeLogin,
  parseAwFixerMetadata,
} from "../aw-fixer-lib.mjs";

const fixture = JSON.parse(
  await readFile(
    new URL("./fixtures/inventory.json", import.meta.url),
    "utf8",
  ),
);
const now = new Date("2026-08-28T06:00:00Z");

test("normalizes GitHub bot logins and filters generated issues", () => {
  assert.equal(normalizeLogin("github-actions[bot]"), "github-actions");
  assert.equal(normalizeLogin("GITHUB-ACTIONS"), "github-actions");
  assert.equal(isGeneratedAwIssue(fixture.issues[0]), true);
  assert.equal(
    isGeneratedAwIssue(
      fixture.issues.find((issue) => issue.user.login === "octocat"),
    ),
    false,
  );
});

test("discovers only current agent source and lock pairs", () => {
  const workflows = discoverCurrentWorkflows(
    fixture.workflowFiles,
    fixture.workflows,
  );
  assert.deepEqual(
    workflows.map((workflow) => workflow.id),
    [10, 11],
  );
});

test("extracts unique run references", () => {
  const references = extractRunReferences(
    "See https://github.com/dotnet/xharness/actions/runs/123, and " +
      "https://github.com/dotnet/xharness/actions/runs/123.",
  );
  assert.deepEqual(references, [
    {
      owner: "dotnet",
      repository: "xharness",
      runId: 123,
      url: "https://github.com/dotnet/xharness/actions/runs/123",
    },
  ]);
});

test("parses only valid aw-fixer fenced metadata", () => {
  const metadata = parseAwFixerMetadata(`
\`\`\`json
{"example": true}
\`\`\`
\`\`\`json
{"aw_fixer":{"schema":1,"kind":"fix-pr","occurrence_keys":["run:1"]}}
\`\`\`
\`\`\`json
{invalid}
\`\`\`
`);
  assert.equal(metadata.length, 1);
  assert.equal(metadata[0].kind, "fix-pr");
  assert.deepEqual(metadata[0].occurrence_keys, ["run:1"]);
});

test("includes all failure-like conclusions", () => {
  assert.deepEqual(
    [...FAILURE_CONCLUSIONS].sort(),
    ["action_required", "cancelled", "failure", "timed_out"],
  );
});

test("builds an ordered manifest and merges run and issue evidence", () => {
  const manifest = buildCandidateManifest(fixture, {
    repository: "dotnet/xharness",
    now,
    lookbackHours: 24,
    mergedDays: 3,
    maxCandidates: 50,
  });

  assert.deepEqual(
    manifest.currentWorkflows.map((workflow) => workflow.id),
    [10, 11],
  );
  assert.deepEqual(manifest.reviewedRunIds, [100, 101, 102, 200]);
  assert.deepEqual(manifest.diagnostics.ignoredWorkflowIds, [12, 13]);
  assert.equal(manifest.summary.signalCount, 8);
  assert.equal(manifest.summary.exactHandledCount, 1);
  assert.equal(manifest.summary.candidateCount, 4);
  assert.equal(manifest.summary.truncated, false);

  assert.equal(manifest.candidates[0].generatedIssue.number, 1700);
  assert.equal(
    manifest.candidates[1].generatedIssue.commentId,
    6002,
  );

  const cancelled = manifest.candidates.find((candidate) =>
    candidate.occurrenceKeys.includes("run:102"),
  );
  assert.equal(cancelled.exactPullRequest.number, 1799);
  assert.deepEqual(
    cancelled.requiredActions.map((action) => action.type).sort(),
    ["add_issue_backlink", "link_issue_to_pull_request"],
  );

  const selfFailure = manifest.candidates.find((candidate) =>
    candidate.occurrenceKeys.includes("run:200"),
  );
  assert.deepEqual(
    selfFailure.signalTypes.sort(),
    ["detection-run", "run"],
  );
  assert.equal(selfFailure.exactPullRequest, undefined);
  assert.equal(selfFailure.generatedIssue.commentId, 6001);
});

test("keeps old open generated issues and ignores human comments", () => {
  const manifest = buildCandidateManifest(fixture, {
    now,
    maxCandidates: 50,
  });

  assert.ok(
    manifest.candidates.some(
      (candidate) => candidate.generatedIssue?.number === 1700,
    ),
  );
  assert.equal(
    manifest.candidates.some((candidate) =>
      candidate.occurrenceKeys.includes("issue-comment:5003"),
    ),
    false,
  );
});

test("applies oldest-first candidate caps", () => {
  const manifest = buildCandidateManifest(fixture, {
    now,
    maxCandidates: 2,
  });

  assert.equal(manifest.candidates.length, 2);
  assert.equal(manifest.summary.candidateCount, 4);
  assert.equal(manifest.summary.returnedCandidateCount, 2);
  assert.equal(manifest.summary.truncated, true);
  assert.equal(manifest.candidates[0].generatedIssue.number, 1700);
  assert.equal(manifest.candidates[1].generatedIssue.commentId, 6002);
});

test("does not treat URL prefixes as exact matches", () => {
  const inventory = {
    workflowFiles: [
      ".github/workflows/active.agent.md",
      ".github/workflows/active.agent.lock.yml",
    ],
    workflows: [
      {
        id: 10,
        path: ".github/workflows/active.agent.lock.yml",
        state: "active",
      },
    ],
    runsByWorkflow: {
      10: [
        {
          id: 123,
          status: "completed",
          conclusion: "failure",
          created_at: "2026-08-28T05:00:00Z",
          html_url: "https://github.com/dotnet/xharness/actions/runs/123",
        },
      ],
    },
    jobsByRun: { 123: [] },
    pullRequests: [
      {
        number: 1900,
        state: "open",
        title: "Fix another run",
        body: "https://github.com/dotnet/xharness/actions/runs/1234",
        html_url: "https://github.com/dotnet/xharness/pull/1900",
      },
    ],
  };

  const manifest = buildCandidateManifest(inventory, { now });
  assert.equal(manifest.candidates.length, 1);
  assert.equal(manifest.candidates[0].exactPullRequest, undefined);
});

test("does not collapse Detection Runs comments onto the parent issue", () => {
  const commentUrl =
    "https://github.com/dotnet/xharness/issues/1697#issuecomment-9001";
  const inventory = {
    issues: [
      {
        id: 1697,
        number: 1697,
        title: "[aw] Detection Runs",
        body: "<!-- gh-aw-detection-runs -->",
        created_at: "2026-08-01T00:00:00Z",
        html_url: "https://github.com/dotnet/xharness/issues/1697",
        user: { login: "github-actions[bot]" },
        labels: ["agentic-workflows"],
      },
    ],
    commentsByIssue: {
      1697: [
        {
          id: 9001,
          body: "A distinct detection occurrence.",
          created_at: "2026-08-27T01:00:00Z",
          html_url: commentUrl,
          user: { login: "github-actions[bot]" },
        },
        {
          id: 9002,
          body: "Tracked by https://github.com/dotnet/xharness/pull/1901",
          created_at: "2026-08-27T02:00:00Z",
          user: { login: "vitek-karas" },
        },
      ],
    },
    pullRequests: [
      {
        number: 1901,
        state: "open",
        title: "Parent issue reference only",
        body: "https://github.com/dotnet/xharness/issues/1697",
        html_url: "https://github.com/dotnet/xharness/pull/1901",
      },
    ],
  };

  let manifest = buildCandidateManifest(inventory, { now });
  assert.equal(manifest.candidates.length, 1);
  assert.equal(manifest.candidates[0].exactPullRequest, undefined);

  inventory.pullRequests[0].body += `\n${commentUrl}`;
  manifest = buildCandidateManifest(inventory, { now });
  assert.equal(manifest.candidates[0].exactPullRequest.number, 1901);
  assert.deepEqual(
    manifest.candidates[0].requiredActions.map((action) => action.type),
    ["add_issue_backlink"],
  );
});

test("repairs missing managed PR comments idempotently", () => {
  const inventory = structuredClone(fixture);
  inventory.commentsByPullRequest["1800"] =
    inventory.commentsByPullRequest["1800"].filter(
      (comment) => !comment.body.includes('"kind":"next-action"'),
    );

  const manifest = buildCandidateManifest(inventory, { now });
  const candidate = manifest.candidates.find((item) =>
    item.occurrenceKeys.includes("run:100"),
  );

  assert.equal(candidate.exactPullRequest.number, 1800);
  assert.deepEqual(candidate.requiredActions, [
    {
      type: "add_pr_comment",
      kind: "next-action",
      pullRequestUrl: "https://github.com/dotnet/xharness/pull/1800",
    },
  ]);
});

test("repairs managed PR comments after source evidence ages out", () => {
  const inventory = {
    pullRequests: [
      {
        number: 1902,
        state: "open",
        title: "[aw-fixer] Handoff for an older run",
        body: "```json\n{\"aw_fixer\":{\"schema\":1,\"kind\":\"handoff-pr\",\"occurrence_keys\":[\"run:42\"],\"incident_fingerprint\":\"older-failure\",\"source_runs\":[\"https://github.com/dotnet/xharness/actions/runs/42\"],\"source_issues\":[]}}\n```",
        created_at: "2026-08-20T00:00:00Z",
        html_url: "https://github.com/dotnet/xharness/pull/1902",
        labels: ["agentic-workflows"],
      },
    ],
    commentsByPullRequest: { 1902: [] },
  };

  const manifest = buildCandidateManifest(inventory, { now });
  assert.equal(manifest.candidates.length, 1);
  assert.equal(manifest.candidates[0].type, "managed-pr-repair");
  assert.deepEqual(
    manifest.candidates[0].requiredActions.map(
      (action) => `${action.type}:${action.kind}`,
    ),
    ["add_pr_comment:analysis", "add_pr_comment:next-action"],
  );
});

test("repairs a missing managed PR label", () => {
  const inventory = structuredClone(fixture);
  inventory.pullRequests.find(
    (pullRequest) => pullRequest.number === 1800,
  ).labels = [];

  const manifest = buildCandidateManifest(inventory, { now });
  const candidate = manifest.candidates.find((item) =>
    item.occurrenceKeys.includes("run:100"),
  );

  assert.deepEqual(candidate.requiredActions, [
    {
      type: "add_pull_request_label",
      label: "agentic-workflows",
      pullRequestUrl: "https://github.com/dotnet/xharness/pull/1800",
    },
  ]);
});

test("rejects unbounded collection options", () => {
  assert.throws(
    () => buildCandidateManifest(fixture, { now, lookbackHours: 169 }),
    /lookbackHours/,
  );
  assert.throws(
    () => buildCandidateManifest(fixture, { now, maxCandidates: 101 }),
    /maxCandidates/,
  );
});
