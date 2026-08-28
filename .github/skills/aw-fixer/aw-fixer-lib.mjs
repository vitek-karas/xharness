import { createHash } from "node:crypto";

export const FAILURE_CONCLUSIONS = new Set([
  "action_required",
  "cancelled",
  "failure",
  "timed_out",
]);

const DETECTION_RUNS_MARKER = "<!-- gh-aw-detection-runs -->";
const RUN_URL_PATTERN =
  /https:\/\/github\.com\/([^/\s]+)\/([^/\s]+)\/actions\/runs\/(\d+)/gi;

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function normalizeUrl(url) {
  return String(url ?? "").replace(/\/+$/, "");
}

function extractUrls(text) {
  const matches = String(text ?? "").match(/https?:\/\/[^\s<>"'`]+/gi) ?? [];
  return new Set(
    matches.map((url) =>
      normalizeUrl(url.replace(/[\]),.;:!?}]+$/, "")),
    ),
  );
}

function textHasExactUrl(text, url) {
  return extractUrls(text).has(normalizeUrl(url));
}

function labels(issue) {
  return (issue.labels ?? []).map((label) =>
    typeof label === "string" ? label : label.name,
  );
}

function timestamp(value) {
  const result = Date.parse(value ?? "");
  return Number.isNaN(result) ? 0 : result;
}

function normalizeText(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/[0-9a-f]{7,40}/g, "<sha>")
    .replace(/\d+/g, "<n>")
    .replace(/\s+/g, " ")
    .trim();
}

function runIdFromUrl(url) {
  const match = String(url ?? "").match(/\/actions\/runs\/(\d+)/i);
  return match?.[1];
}

function parentIssueUrl(url) {
  const match = String(url ?? "").match(
    /^(https:\/\/github\.com\/[^/]+\/[^/]+\/issues\/\d+)/i,
  );
  return match?.[1];
}

function issueNumberFromUrl(url) {
  const match = String(url ?? "").match(/\/issues\/(\d+)/i);
  return match ? Number(match[1]) : undefined;
}

function mergeObjects(left, right) {
  const result = { ...left };
  for (const [key, value] of Object.entries(right ?? {})) {
    if (result[key] === undefined || result[key] === null || result[key] === "") {
      result[key] = value;
    }
  }
  return result;
}

export function normalizeLogin(login) {
  return String(login ?? "")
    .trim()
    .toLowerCase()
    .replace(/\[bot\]$/, "");
}

export function isGeneratedAwIssue(issue) {
  return (
    normalizeLogin(issue?.user?.login) === "github-actions" &&
    String(issue?.title ?? "").startsWith("[aw]") &&
    labels(issue).includes("agentic-workflows") &&
    !issue?.pull_request
  );
}

export function discoverCurrentWorkflows(fileNames, apiWorkflows) {
  const normalizedFiles = fileNames.map((file) =>
    typeof file === "string" ? file : file.path,
  );
  const sourceIds = new Set(
    normalizedFiles
      .filter((path) => path?.endsWith(".agent.md"))
      .map((path) => path.slice(0, -".agent.md".length)),
  );
  const lockPaths = new Set(
    normalizedFiles.filter((path) => {
      if (!path?.endsWith(".agent.lock.yml")) {
        return false;
      }
      return sourceIds.has(path.slice(0, -".agent.lock.yml".length));
    }),
  );

  return (apiWorkflows ?? []).filter((workflow) =>
    lockPaths.has(workflow.path),
  );
}

export function extractRunReferences(text) {
  const references = [];
  for (const match of String(text ?? "").matchAll(RUN_URL_PATTERN)) {
    references.push({
      owner: match[1],
      repository: match[2],
      runId: Number(match[3]),
      url: match[0].replace(/[),.;]+$/, ""),
    });
  }
  return references.filter(
    (reference, index) =>
      references.findIndex((other) => other.url === reference.url) === index,
  );
}

export function parseAwFixerMetadata(text) {
  const metadata = [];
  const fencedJsonPattern = /```json\s*([\s\S]*?)```/gi;

  for (const match of String(text ?? "").matchAll(fencedJsonPattern)) {
    try {
      const parsed = JSON.parse(match[1]);
      if (parsed?.aw_fixer && typeof parsed.aw_fixer === "object") {
        metadata.push(parsed.aw_fixer);
      }
    } catch {
      // Other JSON examples in human-authored text are not aw-fixer metadata.
    }
  }

  return metadata;
}

function failedJobSummary(jobs) {
  return (jobs ?? [])
    .filter((job) => FAILURE_CONCLUSIONS.has(job.conclusion))
    .map((job) => ({
      id: job.id,
      name: job.name,
      conclusion: job.conclusion,
      url: job.html_url,
      failedSteps: (job.steps ?? [])
        .filter((step) => FAILURE_CONCLUSIONS.has(step.conclusion))
        .map((step) => ({
          name: step.name,
          number: step.number,
          conclusion: step.conclusion,
        })),
    }));
}

function runSignals(inventory, currentWorkflows, cutoff) {
  const signals = [];

  for (const workflow of currentWorkflows) {
    for (const run of inventory.runsByWorkflow?.[workflow.id] ?? []) {
      if (timestamp(run.created_at) < cutoff || !FAILURE_CONCLUSIONS.has(run.conclusion)) {
        continue;
      }

      signals.push({
        type: "run",
        occurrenceKeys: [`run:${run.id}`],
        sourceRuns: [run.html_url],
        sourceIssues: [],
        occurredAt: run.created_at,
        workflow: {
          id: workflow.id,
          name: workflow.name,
          path: workflow.path,
        },
        conclusion: run.conclusion,
        run: {
          id: run.id,
          attempt: run.run_attempt,
          event: run.event,
          headSha: run.head_sha,
          url: run.html_url,
        },
        failedJobs: failedJobSummary(inventory.jobsByRun?.[run.id]),
      });
    }
  }

  return signals;
}

function issueOccurrenceSignals(issue, comment, isDetectionRuns) {
  const text = comment?.body ?? issue.body ?? "";
  const issueUrl = issue.html_url;
  const occurrenceUrl = comment?.html_url ?? issueUrl;
  const occurrenceKey = comment
    ? `issue-comment:${comment.id}`
    : `issue:${issue.id}`;
  const references = extractRunReferences(text);
  const common = {
    type: isDetectionRuns ? "detection-run" : "generated-issue",
    occurrenceKeys: [occurrenceKey],
    sourceIssues: unique([issueUrl, occurrenceUrl]),
    occurredAt: comment?.created_at ?? issue.created_at,
    generatedIssue: {
      id: issue.id,
      number: issue.number,
      title: issue.title,
      url: issueUrl,
      commentId: comment?.id,
      commentUrl: comment?.html_url,
      detectionRuns: isDetectionRuns,
    },
  };

  if (references.length === 0) {
    return [{ ...common, sourceRuns: [] }];
  }

  return references.map((reference) => ({
    ...common,
    occurrenceKeys: unique([occurrenceKey, `run:${reference.runId}`]),
    sourceRuns: [reference.url],
  }));
}

function issueSignals(inventory) {
  const signals = [];

  for (const issue of (inventory.issues ?? []).filter(isGeneratedAwIssue)) {
    const isDetectionRuns = String(issue.body ?? "").includes(
      DETECTION_RUNS_MARKER,
    );
    const comments = (inventory.commentsByIssue?.[issue.number] ?? []).filter(
      (comment) => normalizeLogin(comment?.user?.login) === "github-actions",
    );

    if (!isDetectionRuns) {
      signals.push(...issueOccurrenceSignals(issue, undefined, false));
    }
    for (const comment of comments) {
      signals.push(...issueOccurrenceSignals(issue, comment, isDetectionRuns));
    }
  }

  return signals;
}

function signalGroupKey(signal) {
  const runId = runIdFromUrl(signal.sourceRuns?.[0]);
  return runId ? `run:${runId}` : signal.occurrenceKeys[0];
}

export function mergeSignals(signals) {
  const groups = new Map();

  for (const signal of signals) {
    const key = signalGroupKey(signal);
    const current = groups.get(key);
    if (!current) {
      groups.set(key, {
        ...signal,
        signalTypes: [signal.type],
      });
      continue;
    }

    groups.set(key, {
      ...current,
      occurrenceKeys: unique([
        ...current.occurrenceKeys,
        ...signal.occurrenceKeys,
      ]),
      sourceRuns: unique([...current.sourceRuns, ...signal.sourceRuns]),
      sourceIssues: unique([...current.sourceIssues, ...signal.sourceIssues]),
      signalTypes: unique([...current.signalTypes, signal.type]),
      occurredAt:
        timestamp(signal.occurredAt) < timestamp(current.occurredAt)
          ? signal.occurredAt
          : current.occurredAt,
      workflow: mergeObjects(current.workflow, signal.workflow),
      run: mergeObjects(current.run, signal.run),
      generatedIssue: mergeObjects(
        current.generatedIssue,
        signal.generatedIssue,
      ),
      failedJobs:
        current.failedJobs?.length > 0
          ? current.failedJobs
          : signal.failedJobs ?? [],
      conclusion: current.conclusion ?? signal.conclusion,
    });
  }

  return [...groups.values()];
}

function preliminaryFingerprint(signal) {
  const jobNames = (signal.failedJobs ?? []).map((job) => job.name).sort();
  const stepNames = (signal.failedJobs ?? [])
    .flatMap((job) => job.failedSteps.map((step) => step.name))
    .sort();
  const raw = [
    signal.workflow?.id ?? signal.workflow?.path ?? "generated-issue",
    signal.conclusion ?? signal.type,
    ...jobNames,
    ...stepNames,
  ]
    .map(normalizeText)
    .join("|");

  return createHash("sha256").update(raw).digest("hex").slice(0, 20);
}

function pullRequestIndex(pullRequests, commentsByPullRequest, now, mergedDays) {
  const mergedCutoff = now - mergedDays * 24 * 60 * 60 * 1000;

  return (pullRequests ?? [])
    .filter(
      (pullRequest) =>
        pullRequest.state === "open" ||
        (pullRequest.merged_at &&
          timestamp(pullRequest.merged_at) >= mergedCutoff),
    )
    .map((pullRequest) => {
      const comments = commentsByPullRequest?.[pullRequest.number] ?? [];
      const text = [pullRequest.title, pullRequest.body].join("\n");
      const metadata = parseAwFixerMetadata(text);

      return {
        pullRequest,
        text,
        metadata,
        comments,
        managed:
          /^\[aw-fixer\]/i.test(pullRequest.title ?? "") ||
          metadata.length > 0,
        occurrenceKeys: new Set(
          metadata.flatMap((item) => item.occurrence_keys ?? []),
        ),
        sourceUrls: new Set(
          metadata.flatMap((item) => [
            ...(item.source_runs ?? []),
            ...(item.source_issues ?? []),
          ]),
        ),
      };
    });
}

function exactPullRequestMatch(signal, pullRequests) {
  const issueUrls = signal.generatedIssue?.detectionRuns
    ? signal.sourceIssues.filter((url) => url.includes("#issuecomment-"))
    : signal.sourceIssues;
  const urls = [...signal.sourceRuns, ...issueUrls];
  return pullRequests.find(
    (entry) =>
      signal.occurrenceKeys.some((key) => entry.occurrenceKeys.has(key)) ||
      urls.some(
        (url) =>
          entry.sourceUrls.has(url) || textHasExactUrl(entry.text, url),
      ),
  );
}

function issueBacklinkExists(
  signal,
  issueNumber,
  pullRequestUrl,
  commentsByIssue,
) {
  return (commentsByIssue?.[issueNumber] ?? []).some((comment) => {
    const text = String(comment.body ?? "");
    if (!textHasExactUrl(text, pullRequestUrl)) {
      return false;
    }

    if (!signal.generatedIssue?.detectionRuns) {
      return true;
    }

    const commentUrl = signal.generatedIssue.commentUrl;
    if (commentUrl && textHasExactUrl(text, commentUrl)) {
      return true;
    }

    return parseAwFixerMetadata(text).some((metadata) =>
      metadata.occurrence_keys?.some((key) =>
        signal.occurrenceKeys.includes(key),
      ),
    );
  });
}

function pullRequestCommentExists(signal, pullRequestEntry, kind) {
  return pullRequestEntry.comments.some((comment) => {
    if (normalizeLogin(comment.user?.login) !== "vitek-karas") {
      return false;
    }

    return parseAwFixerMetadata(comment.body).some(
      (metadata) =>
        metadata.kind === kind &&
        ((signal.preliminaryFingerprint &&
          metadata.incident_fingerprint ===
            signal.preliminaryFingerprint) ||
          metadata.occurrence_keys?.some((key) =>
            signal.occurrenceKeys.includes(key),
          )),
    );
  });
}

function managedPullRequestActions(signal, pullRequestEntry) {
  if (
    pullRequestEntry.pullRequest.state !== "open" ||
    !pullRequestEntry.managed
  ) {
    return [];
  }

  const actions = [];
  if (
    !labels(pullRequestEntry.pullRequest).includes(
      "agentic-workflows",
    )
  ) {
    actions.push({
      type: "add_pull_request_label",
      label: "agentic-workflows",
      pullRequestUrl: pullRequestEntry.pullRequest.html_url,
    });
  }

  for (const kind of ["analysis", "next-action"]) {
    if (!pullRequestCommentExists(signal, pullRequestEntry, kind)) {
      actions.push({
        type: "add_pr_comment",
        kind,
        pullRequestUrl: pullRequestEntry.pullRequest.html_url,
      });
    }
  }

  return actions;
}

function linkActions(signal, pullRequestEntry, commentsByIssue) {
  if (!pullRequestEntry) {
    return [];
  }

  const actions = [];
  const issueUrls = unique(signal.sourceIssues.map(parentIssueUrl));
  for (const issueUrl of issueUrls) {
    const detectionCommentUrl =
      signal.generatedIssue?.detectionRuns &&
      signal.generatedIssue.url === issueUrl
        ? signal.generatedIssue.commentUrl
        : undefined;
    const requiredUrls = unique([issueUrl, detectionCommentUrl]);
    if (
      requiredUrls.some(
        (url) =>
          !pullRequestEntry.sourceUrls.has(url) &&
          !textHasExactUrl(pullRequestEntry.text, url),
      )
    ) {
      actions.push({
        type: "link_issue_to_pull_request",
        issueUrl,
        commentUrl: detectionCommentUrl,
        pullRequestUrl: pullRequestEntry.pullRequest.html_url,
      });
    }

    const issueNumber = issueNumberFromUrl(issueUrl);
    if (
      issueNumber &&
      !issueBacklinkExists(
        signal,
        issueNumber,
        pullRequestEntry.pullRequest.html_url,
        commentsByIssue,
      )
    ) {
      actions.push({
        type: "add_issue_backlink",
        issueUrl,
        commentUrl: detectionCommentUrl,
        pullRequestUrl: pullRequestEntry.pullRequest.html_url,
      });
    }
  }

  actions.push(...managedPullRequestActions(signal, pullRequestEntry));

  return actions;
}

function managedPullRequestRepairCandidate(pullRequestEntry) {
  if (
    pullRequestEntry.pullRequest.state !== "open" ||
    !pullRequestEntry.managed ||
    pullRequestEntry.metadata.length === 0
  ) {
    return undefined;
  }

  const signal = {
    type: "managed-pr-repair",
    occurrenceKeys: [...pullRequestEntry.occurrenceKeys],
    sourceRuns: unique(
      pullRequestEntry.metadata.flatMap(
        (metadata) => metadata.source_runs ?? [],
      ),
    ),
    sourceIssues: unique(
      pullRequestEntry.metadata.flatMap(
        (metadata) => metadata.source_issues ?? [],
      ),
    ),
    occurredAt:
      pullRequestEntry.pullRequest.created_at ??
      pullRequestEntry.pullRequest.updated_at,
    preliminaryFingerprint: pullRequestEntry.metadata.find(
      (metadata) => metadata.incident_fingerprint,
    )?.incident_fingerprint,
  };
  const requiredActions = managedPullRequestActions(
    signal,
    pullRequestEntry,
  );

  if (requiredActions.length === 0) {
    return undefined;
  }

  return {
    ...signal,
    exactPullRequest: {
      number: pullRequestEntry.pullRequest.number,
      state: pullRequestEntry.pullRequest.state,
      mergedAt: pullRequestEntry.pullRequest.merged_at,
      url: pullRequestEntry.pullRequest.html_url,
    },
    requiredActions,
  };
}

export function buildCandidateManifest(
  inventory,
  {
    repository = "dotnet/xharness",
    now = new Date(),
    lookbackHours = 24,
    mergedDays = 3,
    maxCandidates = 50,
  } = {},
) {
  const nowValue = now instanceof Date ? now : new Date(now);
  if (Number.isNaN(nowValue.valueOf())) {
    throw new Error("now must be a valid date");
  }
  if (lookbackHours < 1 || lookbackHours > 168) {
    throw new Error("lookbackHours must be between 1 and 168");
  }
  if (maxCandidates < 1 || maxCandidates > 100) {
    throw new Error("maxCandidates must be between 1 and 100");
  }

  const currentWorkflows = discoverCurrentWorkflows(
    inventory.workflowFiles ?? [],
    inventory.workflows ?? [],
  );
  const workflowIds = new Set(currentWorkflows.map((workflow) => workflow.id));
  const cutoff = nowValue.valueOf() - lookbackHours * 60 * 60 * 1000;
  const allSignals = [
    ...runSignals(inventory, currentWorkflows, cutoff),
    ...issueSignals(inventory),
  ];
  const pullRequests = pullRequestIndex(
    inventory.pullRequests,
    inventory.commentsByPullRequest,
    nowValue.valueOf(),
    mergedDays,
  );

  const candidates = [];
  let exactHandled = 0;
  for (const mergedSignal of mergeSignals(allSignals)) {
    const signal = {
      ...mergedSignal,
      preliminaryFingerprint: preliminaryFingerprint(mergedSignal),
    };
    const match = exactPullRequestMatch(signal, pullRequests);
    const requiredActions = linkActions(
      signal,
      match,
      inventory.commentsByIssue,
    );
    if (match && requiredActions.length === 0) {
      exactHandled += 1;
      continue;
    }

    candidates.push({
      ...signal,
      exactPullRequest: match
        ? {
            number: match.pullRequest.number,
            state: match.pullRequest.state,
            mergedAt: match.pullRequest.merged_at,
            url: match.pullRequest.html_url,
          }
        : undefined,
      requiredActions,
    });
  }

  const candidatePullRequestNumbers = new Set(
    candidates
      .map((candidate) => candidate.exactPullRequest?.number)
      .filter(Boolean),
  );
  for (const pullRequest of pullRequests) {
    if (candidatePullRequestNumbers.has(pullRequest.pullRequest.number)) {
      continue;
    }

    const repairCandidate = managedPullRequestRepairCandidate(pullRequest);
    if (repairCandidate) {
      candidates.push(repairCandidate);
    }
  }

  candidates.sort(
    (left, right) => timestamp(left.occurredAt) - timestamp(right.occurredAt),
  );

  const reviewedRunIds = unique(
    currentWorkflows.flatMap((workflow) =>
      (inventory.runsByWorkflow?.[workflow.id] ?? [])
        .filter((run) => timestamp(run.created_at) >= cutoff)
        .map((run) => run.id),
    ),
  );

  return {
    schema: 1,
    repository,
    generatedAt: nowValue.toISOString(),
    lookbackHours,
    mergedDays,
    currentWorkflows: currentWorkflows.map((workflow) => ({
      id: workflow.id,
      name: workflow.name,
      path: workflow.path,
    })),
    reviewedRunIds,
    candidates: candidates.slice(0, maxCandidates),
    summary: {
      currentWorkflowCount: currentWorkflows.length,
      reviewedRunCount: reviewedRunIds.length,
      signalCount: allSignals.length,
      exactHandledCount: exactHandled,
      candidateCount: candidates.length,
      returnedCandidateCount: Math.min(candidates.length, maxCandidates),
      truncated: candidates.length > maxCandidates,
    },
    diagnostics: {
      ignoredWorkflowIds: (inventory.workflows ?? [])
        .filter((workflow) => !workflowIds.has(workflow.id))
        .map((workflow) => workflow.id),
    },
  };
}
