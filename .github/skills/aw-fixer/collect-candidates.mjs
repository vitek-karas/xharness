#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import process from "node:process";

import {
  FAILURE_CONCLUSIONS,
  buildCandidateManifest,
  discoverCurrentWorkflows,
  isGeneratedAwIssue,
} from "./aw-fixer-lib.mjs";

function usage() {
  return [
    "Usage: node collect-candidates.mjs [options]",
    "",
    "Options:",
    "  --repo OWNER/REPO          Repository to inspect (default: dotnet/xharness)",
    "  --expected-login LOGIN     Required gh identity (default: vitek-karas)",
    "  --lookback-hours HOURS     Run lookback, 1-168 (default: 24)",
    "  --merged-days DAYS         Recently merged PR window (default: 3)",
    "  --max-candidates COUNT     Manifest candidate cap, 1-100 (default: 50)",
    "  --now ISO_TIMESTAMP        Deterministic current time for testing",
    "  --output PATH              Write JSON to PATH instead of stdout",
    "  --help                     Show this help",
  ].join("\n");
}

function parseInteger(name, value, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
  return parsed;
}

function parseArguments(argv) {
  const options = {
    repository: "dotnet/xharness",
    expectedLogin: "vitek-karas",
    lookbackHours: 24,
    mergedDays: 3,
    maxCandidates: 50,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    const value = argv[index + 1];
    switch (argument) {
      case "--repo":
        options.repository = value;
        index += 1;
        break;
      case "--expected-login":
        options.expectedLogin = value;
        index += 1;
        break;
      case "--lookback-hours":
        options.lookbackHours = parseInteger(argument, value, 1, 168);
        index += 1;
        break;
      case "--merged-days":
        options.mergedDays = parseInteger(argument, value, 1, 30);
        index += 1;
        break;
      case "--max-candidates":
        options.maxCandidates = parseInteger(argument, value, 1, 100);
        index += 1;
        break;
      case "--now":
        options.now = value;
        index += 1;
        break;
      case "--output":
        options.output = value;
        index += 1;
        break;
      case "--help":
        options.help = true;
        break;
      default:
        throw new Error(`Unknown argument: ${argument}`);
    }
  }

  if (!/^[^/\s]+\/[^/\s]+$/.test(options.repository)) {
    throw new Error("--repo must use OWNER/REPO format");
  }
  if (!options.expectedLogin) {
    throw new Error("--expected-login must not be empty");
  }
  if (options.now && Number.isNaN(Date.parse(options.now))) {
    throw new Error("--now must be an ISO timestamp");
  }

  return options;
}

function ghEnvironment() {
  const environment = { ...process.env };
  delete environment.GH_TOKEN;
  delete environment.GITHUB_TOKEN;
  return environment;
}

function ghApi(endpoint, { paginate = false } = {}) {
  const arguments_ = ["api"];
  if (paginate) {
    arguments_.push("--paginate", "--slurp");
  }
  arguments_.push(endpoint);

  const result = spawnSync("gh", arguments_, {
    encoding: "utf8",
    env: ghEnvironment(),
    maxBuffer: 32 * 1024 * 1024,
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`Unable to execute gh: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const detail = String(result.stderr ?? "").trim();
    throw new Error(`gh api failed for ${endpoint}: ${detail || `exit ${result.status}`}`);
  }

  try {
    return JSON.parse(result.stdout);
  } catch {
    throw new Error(`gh api returned invalid JSON for ${endpoint}`);
  }
}

function pagedItems(endpoint, property) {
  const pages = ghApi(endpoint, { paginate: true });
  return pages.flatMap((page) => {
    if (Array.isArray(page)) {
      return page;
    }
    return page?.[property] ?? [];
  });
}

function query(endpoint, parameters = {}) {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(parameters)) {
    if (value !== undefined) {
      search.set(name, String(value));
    }
  }
  return `${endpoint}?${search}`;
}

function collectInventory(repository, now, lookbackHours, mergedDays) {
  const repositoryData = ghApi(`repos/${repository}`);
  const workflowEntries = ghApi(
    query(`repos/${repository}/contents/.github/workflows`, {
      ref: repositoryData.default_branch,
    }),
  );
  if (!Array.isArray(workflowEntries)) {
    throw new Error(".github/workflows did not resolve to a directory");
  }

  const workflows = pagedItems(
    query(`repos/${repository}/actions/workflows`, { per_page: 100 }),
    "workflows",
  );
  const workflowFiles = workflowEntries.map((entry) => entry.path);
  const currentWorkflows = discoverCurrentWorkflows(workflowFiles, workflows);
  const createdAfter = new Date(
    now.valueOf() - lookbackHours * 60 * 60 * 1000,
  ).toISOString();
  const runsByWorkflow = {};
  const jobsByRun = {};

  for (const workflow of currentWorkflows) {
    const runs = pagedItems(
      query(`repos/${repository}/actions/workflows/${workflow.id}/runs`, {
        created: `>=${createdAfter}`,
        per_page: 100,
      }),
      "workflow_runs",
    ).filter((run) => run.status === "completed" || Boolean(run.conclusion));
    runsByWorkflow[workflow.id] = runs;

    for (const run of runs.filter((item) =>
      FAILURE_CONCLUSIONS.has(item.conclusion),
    )) {
      jobsByRun[run.id] = pagedItems(
        query(
          `repos/${repository}/actions/runs/${run.id}/attempts/${run.run_attempt ?? 1}/jobs`,
          { per_page: 100 },
        ),
        "jobs",
      );
    }
  }

  const issues = pagedItems(
    query(`repos/${repository}/issues`, {
      state: "open",
      labels: "agentic-workflows",
      per_page: 100,
    }),
  ).filter(isGeneratedAwIssue);
  const commentsByIssue = {};
  for (const issue of issues) {
    commentsByIssue[issue.number] = pagedItems(
      query(`repos/${repository}/issues/${issue.number}/comments`, {
        per_page: 100,
      }),
    );
  }

  const openPullRequests = pagedItems(
    query(`repos/${repository}/pulls`, {
      state: "open",
      per_page: 100,
    }),
  );
  const mergedSearch = pagedItems(
    query("search/issues", {
      q: `repo:${repository} is:pr is:merged merged:>=${new Date(
        now.valueOf() - mergedDays * 24 * 60 * 60 * 1000,
      )
        .toISOString()
        .slice(0, 10)}`,
      per_page: 100,
    }),
    "items",
  );
  const mergedPullRequests = mergedSearch.map((item) => ({
    ...item,
    state: "closed",
    merged_at: item.closed_at,
  }));
  const pullRequests = [...openPullRequests, ...mergedPullRequests];
  const commentsByPullRequest = {};
  for (const pullRequest of pullRequests) {
    if (!/^\[aw-fixer\]/i.test(pullRequest.title ?? "")) {
      continue;
    }

    commentsByPullRequest[pullRequest.number] = pagedItems(
      query(`repos/${repository}/issues/${pullRequest.number}/comments`, {
        per_page: 100,
      }),
    );
  }

  return {
    workflowFiles,
    workflows,
    runsByWorkflow,
    jobsByRun,
    issues,
    commentsByIssue,
    pullRequests,
    commentsByPullRequest,
  };
}

function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }

  const identity = ghApi("user");
  if (identity.login !== options.expectedLogin) {
    throw new Error(
      `Expected gh identity ${options.expectedLogin}, got ${identity.login}`,
    );
  }

  const now = new Date(options.now ?? Date.now());
  const inventory = collectInventory(
    options.repository,
    now,
    options.lookbackHours,
    options.mergedDays,
  );
  const manifest = buildCandidateManifest(inventory, {
    repository: options.repository,
    now,
    lookbackHours: options.lookbackHours,
    mergedDays: options.mergedDays,
    maxCandidates: options.maxCandidates,
  });
  const output = `${JSON.stringify(manifest, null, 2)}\n`;

  if (options.output) {
    writeFileSync(options.output, output, { encoding: "utf8", flag: "wx" });
  } else {
    process.stdout.write(output);
  }
}

try {
  main();
} catch (error) {
  process.stderr.write(`aw-fixer collector failed: ${error.message}\n`);
  process.exitCode = 1;
}
