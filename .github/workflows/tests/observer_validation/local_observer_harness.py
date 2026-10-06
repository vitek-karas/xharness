#!/usr/bin/env python3
"""Run original/prepared observers in Docker and compare captured evidence.

GitHub reads use fixtures and safe-output requests are captured locally.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MODULE_PATH = Path(__file__).resolve()
ROOT = (
    _MODULE_PATH.parents[4]
    if len(_MODULE_PATH.parents) > 4
    else Path.cwd()
)
ORIGINAL_COMMIT = "4f2266d4c147bd8af3fe4dff129f725ddb9dce57"
ORIGINAL_INPUTS = {
    "observer-prompt.md": ".github/workflows/runtime-failure-observer.agent.md",
    "observer-http": ".github/workflows/runtime-failure-observer-http",
    "observer-lock.yml": ".github/workflows/runtime-failure-observer.agent.lock.yml",
}
DEFINITIONS = (154, 223, 224, 225, 226, 228, 260, 261, 265)
TARGET_EXIT_CODES = {70, 71, 78, 79, 80, 81, 82, 83}
COMMAND_RE = re.compile(
    r"(?:xharness\s+(?:apple|android|wasm)\b|"
    r"dotnet\s+exec\s+\S*Microsoft\.DotNet\.XHarness\.CLI\.dll\b)",
    re.IGNORECASE,
)
EXIT_RE = re.compile(
    r"(?:Exit code:\s*|exited with code\s*|ExitCode=)(-?\d+)", re.IGNORECASE
)
VERSION_RE = re.compile(r"\+([0-9a-f]{40})(?:\b|$)", re.IGNORECASE)
GUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
ABSOLUTE_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._-]+)+")


class FixtureMiss(RuntimeError):
    """Raised when an evaluation asks for an unrecorded MCP request."""


class FixtureResponseError(RuntimeError):
    """Raised when a recorded MCP occurrence is an explicit protocol error."""


class FixtureGitHubMCP:
    """Keyed, occurrence-aware fixture for the pinned GitHub MCP read surface.

    Fixtures intentionally match the tool name and canonical JSON arguments.  A
    missing key is an evaluation failure; it must never turn into an empty
    result or a live request.
    """

    TOOL_SCHEMAS = {
        "search_issues": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "page": {"type": "integer"},
                "perPage": {"type": "integer"},
            },
            "required": ["query"],
        },
        "search_pull_requests": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "page": {"type": "integer"},
                "perPage": {"type": "integer"},
            },
            "required": ["query"],
        },
        "search_code": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "page": {"type": "integer"},
                "perPage": {"type": "integer"},
            },
            "required": ["q"],
        },
        "list_issues": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string"},
                "after": {"type": ["string", "null"]},
                "first": {"type": "integer"},
            },
            "required": ["owner", "repo"],
        },
        "list_pull_requests": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string"},
                "after": {"type": ["string", "null"]},
                "first": {"type": "integer"},
            },
            "required": ["owner", "repo"],
        },
        "issue_read": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["get", "get_comments", "get_sub_issues", "get_parent", "get_labels"]},
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "page": {"type": "integer"},
                "perPage": {"type": "integer"},
            },
            "required": ["method", "owner", "repo", "issue_number"],
        },
        "pull_request_read": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["get", "get_diff", "get_status", "get_files", "get_commits", "get_review_comments", "get_reviews", "get_comments", "get_check_runs"]},
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pullNumber": {"type": "integer"},
                "page": {"type": "integer"},
                "perPage": {"type": "integer"},
                "after": {"type": "string"},
            },
            "required": ["method", "owner", "repo", "pullNumber"],
        },
        "get_file_contents": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["owner", "repo", "path"],
        },
        "get_commit": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "sha": {"type": "string"},
            },
            "required": ["owner", "repo", "sha"],
        },
    }

    def __init__(
        self,
        fixture: dict[str, Any],
        calls_path: Path | None = None,
        allowed_tools: set[str] | None = None,
    ) -> None:
        self.fixture = fixture
        self.calls_path = calls_path
        self.allowed_tools = allowed_tools or set(self.TOOL_SCHEMAS)
        self.calls: list[dict[str, Any]] = []
        self._occurrences: dict[str, list[Any]] = {}
        self._positions: dict[str, int] = {}
        self._load_requests(fixture)

    @staticmethod
    def _canonical(tool: str, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {"tool": tool, "arguments": arguments},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _load_requests(self, fixture: dict[str, Any]) -> None:
        entries = fixture.get("requests")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("fixture request entries must be objects")
                tool = entry.get("tool")
                arguments = entry.get("arguments", {})
                if not isinstance(tool, str) or not isinstance(arguments, dict):
                    raise ValueError("fixture request requires tool and object arguments")
                responses = entry.get("responses")
                if responses is None:
                    responses = [entry.get("response")]
                if not isinstance(responses, list) or not responses:
                    raise ValueError("fixture request requires a non-empty responses array")
                self._occurrences.setdefault(
                    self._canonical(tool, arguments), []
                ).extend(responses)
            return

        # The nested form is convenient for hand-authored fixtures:
        # {"tools": {"search_issues": [{"arguments": ..., "responses": [...]}]}}.
        tools = fixture.get("tools")
        if isinstance(tools, dict):
            for tool, tool_entries in tools.items():
                if not isinstance(tool_entries, list):
                    raise ValueError(f"fixture entries for {tool} must be an array")
                for entry in tool_entries:
                    if not isinstance(entry, dict):
                        raise ValueError("fixture tool entries must be objects")
                    arguments = entry.get("arguments", {})
                    responses = entry.get("responses")
                    if not isinstance(arguments, dict) or not isinstance(responses, list):
                        raise ValueError(
                            "fixture tool entry requires object arguments and responses"
                        )
                    self._occurrences.setdefault(
                        self._canonical(tool, arguments), []
                    ).extend(responses)
            return

        raise ValueError(
            "fixture must use keyed requests or tools entries; "
            "unkeyed empty/default responses are not supported"
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": f"Fixture-backed read-only GitHub MCP tool: {name}.",
                "inputSchema": schema,
            }
            for name, schema in self.TOOL_SCHEMAS.items()
            if name in self.allowed_tools
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        item = {"tool": name, "arguments": arguments}
        self.calls.append(item)
        try:
            response = self._lookup(name, arguments)
            item["response"] = response
            return response
        except (FixtureMiss, FixtureResponseError) as error:
            item["error"] = str(error)
            raise
        finally:
            if self.calls_path is not None:
                self.calls_path.parent.mkdir(parents=True, exist_ok=True)
                with self.calls_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, sort_keys=True) + "\n")

    def _lookup(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.TOOL_SCHEMAS or name not in self.allowed_tools:
            raise FixtureMiss(f"fixture MCP tool is not available: {name}")
        key = self._canonical(name, arguments)
        values = self._occurrences.get(key)
        if values is None:
            available = sorted(
                candidate
                for candidate in self._occurrences
                if json.loads(candidate).get("tool") == name
            )
            raise FixtureMiss(
                f"fixture miss for {name} {json.dumps(arguments, sort_keys=True)}; "
                f"available keys for tool: {available}"
            )
        position = self._positions.get(key, 0)
        if position >= len(values):
            raise FixtureMiss(
                f"fixture occurrence exhausted for {name} "
                f"{json.dumps(arguments, sort_keys=True)}"
            )
        self._positions[key] = position + 1
        response = values[position]
        if isinstance(response, dict) and "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                raise FixtureResponseError(
                    f"{error.get('code', 'fixture_error')}: "
                    f"{error.get('message', 'recorded fixture error')}"
                )
            raise FixtureResponseError(str(error))
        return response


def freeze_original_inputs(run_root: Path) -> dict[str, str]:
    input_root = run_root / "inputs"
    input_root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for name, repository_path in ORIGINAL_INPUTS.items():
        target = input_root / name
        target.write_text(
            subprocess.check_output(
                ["git", "show", f"{ORIGINAL_COMMIT}:{repository_path}"],
                cwd=ROOT,
                text=True,
            ),
            encoding="utf-8",
        )
        manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (input_root / "manifest.json").write_text(
        json.dumps({"commit": ORIGINAL_COMMIT, "files": manifest}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _sanitize(value: str) -> str:
    value = GUID_RE.sub("<guid>", value)
    value = ABSOLUTE_PATH_RE.sub("<path>", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_candidate(console: str) -> dict[str, Any] | None:
    if not COMMAND_RE.search(console):
        return None
    lines = console.splitlines()
    command_lines = [line for line in lines if "running command" in line.casefold()]
    if not command_lines:
        command_lines = [line for line in lines if COMMAND_RE.search(line)]
    exit_match = EXIT_RE.search(console)
    if exit_match is None:
        return None
    exit_code = int(exit_match.group(1))
    before_exit = lines[: next((index for index, line in enumerate(lines) if EXIT_RE.search(line)), len(lines))]
    stderr = [line for line in before_exit[-50:] if line.strip()]
    signature = next(
        (
            _sanitize(line)
            for line in stderr
            if "xharness" not in line.casefold() and "running command" not in line.casefold()
        ),
        "no non-banner stderr line",
    )
    version = VERSION_RE.search(console)
    return {
        "exit_code": exit_code,
        "command": _sanitize(command_lines[-1]),
        "signature": signature,
        "version_sha": version.group(1) if version else None,
        "stderr_tail": [_sanitize(line) for line in stderr[-20:]],
        "improvement_target": exit_code in TARGET_EXIT_CODES,
    }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_isolated_checkout(
    run_root: Path, revision: str, source_repository: Path | None = None
) -> Path:
    checkout = run_root / "xharness-checkout"
    if checkout.exists():
        raise RuntimeError(f"isolated checkout already exists: {checkout}")
    result = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--local",
            "--no-hardlinks",
            str(source_repository or ROOT),
            str(checkout),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not clone isolated checkout")
    result = subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", revision],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not check out {revision}")
    return checkout


def _prepare_docker_runtime(
    run_root: Path,
    *,
    fixture_path: Path,
    github_profile: str,
    revision: str = ORIGINAL_COMMIT,
    source_repository: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Prepare a Linux Copilot runtime with a private container /tmp."""

    run_root = run_root.resolve()
    fixture_path = fixture_path.resolve()
    agent_root = run_root / "agent-state"
    runner_temp = run_root / "runner-temp"
    copilot_home = run_root / "copilot-home"
    for path in (agent_root, runner_temp, copilot_home, run_root / "native-tmp"):
        path.mkdir(parents=True, exist_ok=True)
    checkout = _prepare_isolated_checkout(run_root, revision, source_repository)

    fixture_copy = run_root / "fixtures" / "github-mcp.json"
    fixture_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture_path, fixture_copy)
    fixture_script = ROOT / ".github/workflows/tests/observer_validation/fixture-github-mcp.py"
    shutil.copy2(fixture_script, run_root / "fixture-github-mcp.py")
    # The fixture imports the keyed request implementation from this module.
    # Copy the harness beside it so the container has no dependency on the
    # host checkout or its Python import path.
    shutil.copy2(Path(__file__).resolve(), run_root / "local_observer_harness.py")

    bin_root = run_root / "bin"
    bin_root.mkdir(parents=True, exist_ok=True)
    blocked = textwrap.dedent(
        """\
        #!/usr/bin/python3
        import os
        import sys
        from pathlib import Path

        path = Path(os.environ["OBSERVER_BLOCKED_COMMANDS"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(repr(sys.argv) + "\\n")
        print(
            "blocked by local observer harness: "
            + os.path.basename(sys.argv[0]),
            file=sys.stderr,
        )
        raise SystemExit(126)
        """
    )
    for name in ("gh", "curl", "wget", "nc"):
        _write_executable(bin_root / name, blocked)
    helper_launcher = textwrap.dedent(
        """\
        #!/usr/bin/python3
        import importlib.machinery
        import importlib.util
        import json
        import os
        import sys
        from datetime import datetime, timezone
        from pathlib import Path

        source = Path("/workspace/inputs/observer-http")
        root = Path(os.environ["OBSERVER_AGENT_ROOT"]).resolve()
        original_root = "/tmp/gh-aw/agent"
        argv = [argument.replace(original_root, str(root)) for argument in sys.argv[1:]]
        record = {
            "argv": sys.argv[1:],
            "mapped_argv": argv,
            "observed_time": datetime.now(timezone.utc).isoformat(),
        }
        requests = Path(os.environ["OBSERVER_HELPER_CALLS"])
        requests.parent.mkdir(parents=True, exist_ok=True)
        with requests.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\\n")
        loader = importlib.machinery.SourceFileLoader(
            "frozen_observer_http", str(source)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None or spec.loader is None:
            raise SystemExit("could not load frozen observer helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.OUTPUT_ROOT = root
        sys.argv = [str(source), *argv]
        try:
            status = module.main()
        except BaseException as error:
            record["error"] = f"{type(error).__name__}: {error}"
            with requests.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\\n")
            raise
        record["exit_code"] = status
        with requests.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\\n")
        raise SystemExit(status)
        """
    )
    _write_executable(bin_root / "runtime-failure-observer-http", helper_launcher)

    allowed_tools = (
        "search_issues,search_pull_requests,issue_read,pull_request_read"
        if github_profile == "issue-pr"
        else ",".join(FixtureGitHubMCP.TOOL_SCHEMAS)
    )
    calls_path = run_root / "github-mcp-calls.jsonl"
    safe_output_path = run_root / "safe-output-capture.jsonl"
    mcp_config = {
        "mcpServers": {
            "github": {
                "type": "local",
                "command": "/usr/bin/python3",
                "args": [
                    "/workspace/fixture-github-mcp.py",
                    "--mode",
                    "github",
                    "--fixture",
                    "/workspace/fixtures/github-mcp.json",
                    "--calls-out",
                    "/workspace/github-mcp-calls.jsonl",
                    "--tools",
                    allowed_tools,
                ],
                "tools": ["*"],
            },
            "safeoutputs": {
                "type": "local",
                "command": "/usr/bin/python3",
                "args": [
                    "/workspace/fixture-github-mcp.py",
                    "--mode",
                    "safeoutputs",
                    "--capture",
                    "/workspace/safe-output-capture.jsonl",
                ],
                "tools": ["*"],
            },
        }
    }
    (copilot_home / "mcp-config.json").write_text(
        json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8"
    )
    (copilot_home / "config.json").write_text(
        json.dumps({"disabledMcpServers": ["github-mcp-server"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return checkout, {
        "mode": "docker-copilot",
        "docker_available": shutil.which("docker") is not None,
        "docker_runner_used": True,
        "image": "observer-validation-copilot:1.0.85-jq",
        "base_image": (
            "ghcr.io/github/gh-aw-firewall/agent@"
            "sha256:1bacef0f405d77999d01d8c00cb731222531b3f43ef9dc8312c513d6d9038bb1"
        ),
        "private_tmpfs": True,
        "github_mcp": {
            "provider": "local-fixture",
            "profile": github_profile,
            "calls": str(calls_path),
            "live_fallback": False,
        },
        "safe_outputs": {
            "provider": "local-capture",
            "path": str(safe_output_path),
            "github_writes": False,
        },
        "checkout": str(checkout),
        "checkout_revision": revision,
        "limitations": [
            "The Copilot CLI runs in the pinned gh-aw Linux base image with a locally installed pinned CLI layer.",
            "The container has a private /tmp tmpfs, but this is not a full gh-aw gateway/firewall orchestration run.",
            "Common direct network helpers are blocked; the frozen helper is the only permitted HTTP path.",
        ],
    }


def _prepare_collected_inputs(
    run_root: Path, bundle: Path, hlx_executable: Path
) -> dict[str, Any]:
    manifest = json.loads((bundle / "observer-manifest.json").read_text())
    index = json.loads((bundle / "failure-index.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("complete") is not True:
        raise ValueError("prepared evaluation requires a complete observer manifest")
    if manifest.get("errors") or not isinstance(index, dict) or index.get("errors"):
        raise ValueError("prepared evaluation cannot ignore evidence errors")
    if not isinstance(index.get("candidates"), list):
        raise ValueError("prepared failure index must contain a candidate array")
    for relative in (manifest["snapshot"]["path"], manifest["source"]["package"]["path"]):
        path = (bundle / relative).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_dir():
            raise ValueError(f"prepared input is absent or outside the bundle: {relative}")
    script = ROOT / ".github/workflows/runtime-failure-observer-collect"
    loader = importlib.machinery.SourceFileLoader("observer_eval_collector", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("could not load the production collector")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    # Re-derive policy fields with current code, without acquiring CI again or
    # altering the authoritative captured bundle.
    collector = object.__new__(module.ObserverCollector)
    collector.definitions = manifest["definitions"]
    collector.candidates = index["candidates"]
    class OfflineProvider(module.HlxV011Provider):
        def _environment(self):
            return {
                **super()._environment(),
                "HLX_EVAL_SNAPSHOT": str((bundle / manifest["snapshot"]["path"]).resolve()),
            }

    provider = OfflineProvider(hlx_executable, run_root / "offline-reindex")
    invocations = {}
    console_references = {}
    for candidate in collector.candidates:
        identity = (candidate["jobId"], candidate["workItemName"])
        if identity not in invocations:
            console = provider.helix_console(*identity)
            if console.outcome != "ok" or console.text is None:
                raise ValueError(f"required offline console unavailable: {identity}")
            invocations[identity] = {
                invocation["lineRange"]["invocation"]: invocation
                for invocation in module.parse_xharness_invocations(console.text)
            }
            console_references[identity] = console.identity
        invocation = invocations[identity][candidate["evidence"]["lineRange"]["invocation"]]
        if invocation["exitCode"] != candidate["xharnessExitCode"]:
            raise ValueError("offline re-index changed invocation exit attribution")
        for field in ("command", "commandNorm", "signature", "signatureNorm"):
            candidate[field] = invocation[field]
        candidate["evidence"]["console"].update({
            field: console_references[identity][field] for field in ("url", "urlKind")
        })
        candidate["exactTuple"] = (
            f"{candidate['xharnessExitCode']}|{candidate['commandNorm']}|"
            f"{candidate['signatureNorm']}"
        )
    collector._compute_recurrence()
    output = run_root / "agent-state" / "observer"
    output.mkdir(parents=True)
    index_data = (json.dumps(index, sort_keys=True, indent=2) + "\n").encode()
    (output / "failure-index.json").write_bytes(index_data)
    manifest["outputs"]["failureIndexSha256"] = hashlib.sha256(index_data).hexdigest()
    analysis_data = (
        json.dumps(module.build_analysis_inputs(manifest, index), sort_keys=True, indent=2)
        + "\n"
    ).encode()
    (output / "analysis-inputs.json").write_bytes(analysis_data)
    (output / "filed.tsv").write_bytes(b"")
    manifest["outputs"]["analysisInputs"] = "analysis-inputs.json"
    manifest["outputs"]["analysisInputsSha256"] = hashlib.sha256(analysis_data).hexdigest()
    (output / "observer-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (run_root / "input-provenance.json").write_text(json.dumps({
        "capturedBundle": str(bundle.resolve()),
        "capturedIndexSha256": hashlib.sha256((bundle / "failure-index.json").read_bytes()).hexdigest(),
        "collectorSha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "derivedIndexSha256": hashlib.sha256(index_data).hexdigest(),
        "offlineConsoleCount": len(invocations),
        "liveCiRequests": 0,
    }, indent=2) + "\n")
    return manifest


def _prepare_analysis_image(run_root: Path) -> str:
    runtime_image = (
        "mcr.microsoft.com/dotnet/runtime:10.0@"
        "sha256:b89586dc17781f25531909993658aa8161205ae38b8cec8847df4a8221a403d5"
    )
    base = "observer-validation-copilot:1.0.85-jq"
    architecture = subprocess.run(
        ["docker", "image", "inspect", base, "--format", "{{.Architecture}}"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    context = run_root / "image-context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(
        f"FROM {runtime_image} AS runtime\nFROM {base}\n"
        "COPY --from=runtime /usr/share/dotnet /opt/dotnet\n"
    )
    image = "observer-validation-copilot:1.0.85-hlx"
    result = subprocess.run(
        ["docker", "build", "--quiet", "--platform", f"linux/{architecture}",
         "--tag", image, "--file", str(dockerfile), str(context)],
        capture_output=True, text=True, timeout=300,
    )
    (run_root / "image-build.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"comparison image build failed; see {run_root / 'image-build.log'}")
    return image


def _materialize_analysis_agents(checkout: Path, source: str) -> str:
    body = source.split("\n---\n", 1)[1]
    blocks = re.findall(
        r"(?ms)^## agent: `([^`]+)`\n---\n(.*?)\n---\n(.*?)(?=^## agent: `|\Z)",
        body,
    )
    if not blocks:
        raise ValueError("workflow has no analysis agent definitions")
    agents = checkout / ".github" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for name, header, instructions in blocks:
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("invalid workflow agent name")
        (agents / f"{name}.agent.md").write_text(f"---\n{header}\n---\n{instructions}")
    return body.split("\n## agent: `", 1)[0]


def _summarize_prepared_run(
    run_root: Path, manifest: dict[str, Any], status: int, fidelity: dict[str, Any]
) -> dict[str, Any]:
    index = json.loads((run_root / "agent-state/observer/failure-index.json").read_text())
    capture = run_root / "safe-output-capture.jsonl"
    outputs = [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []
    errors = []
    if status:
        errors.append({"type": "agent_exit", "exit_code": status})
    if not outputs:
        errors.append({"type": "safe_output_missing"})
    for item in outputs:
        if item["tool"] in {"missing_tool", "missing_data", "report_incomplete"}:
            errors.append({"type": "safe_output_incomplete", "arguments": item["arguments"]})
        elif item["tool"] not in {"noop", "create_pull_request"}:
            errors.append({"type": "safe_output_not_allowed", "tool": item["tool"]})
    pr_count = sum(item["tool"] == "create_pull_request" for item in outputs)
    noop_count = sum(item["tool"] == "noop" for item in outputs)
    if pr_count > 2 or noop_count > 1 or (pr_count and noop_count):
        errors.append({"type": "safe_output_contract_invalid",
                       "pullRequests": pr_count, "noops": noop_count})
    calls = run_root / "github-mcp-calls.jsonl"
    github_calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    errors.extend({"type": "github_fixture_error", "request": item} for item in github_calls if item.get("error"))
    candidates = [item for item in index["candidates"] if item["buildRole"] == "source" and item["improvementTarget"]]
    return {
        "schema_version": 2,
        "execution_kind": "actual-prepared-agent-control-flow",
        "status": "incomplete" if errors else "complete",
        "logical_now": manifest["asOf"],
        "coverage": {
            "definitions_required": len(DEFINITIONS),
            "definitions_requested": manifest["counts"]["definitions"],
            "selected_builds": manifest["counts"]["selectedBuilds"],
            "helix_work_item_inventories": manifest["counts"]["helixJobs"],
            "xharness_candidates": len(candidates),
            "analysis_candidates": sum(item.get("analysisRequired") is True for item in candidates),
        },
        "candidates": [
            {"definition": item["definitionId"], "build_id": item["buildId"],
             "job_id": item["jobId"], "work_item": item["workItemName"],
             "exit_code": item["xharnessExitCode"], "signature": item["signatureNorm"]}
            for item in candidates
        ],
        "recurrence": [
            {"id": item["id"], **item["recurrence"]} for item in candidates
        ],
        "source_revision": manifest["source"]["startingRevision"],
        "errors": errors,
        "safe_output_intents": outputs,
        "agent": {"exit_code": status, "fidelity": fidelity},
    }


def _helper_coverage(records: list[dict[str, Any]]) -> dict[str, int]:
    inventory_jobs = set()
    for record in records:
        arguments = record.get("argv", [])
        if (record.get("exit_code") == 0 and arguments
                and arguments[0] == "helix-work-items" and "--job-id" in arguments):
            inventory_jobs.add(arguments[arguments.index("--job-id") + 1])
    # Each invocation has separate start and completion/error records.
    return {
        "helper_requests": sum("exit_code" not in r and "error" not in r for r in records),
        "helper_request_records": len(records),
        "helix_work_item_inventories": len(inventory_jobs),
    }


def _summarize_actual_run(
    run_root: Path,
    agent_status: int,
    agent_output: str = "",
) -> dict[str, Any]:
    agent_root = run_root / "agent-state"
    calls_path = run_root / "helper-invocations.jsonl"
    helper_calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if calls_path.is_file() else []
    candidates: list[dict[str, Any]] = []
    for call in helper_calls:
        if "helix-console" not in " ".join(call.get("argv", [])):
            continue
        if call.get("exit_code") != 0:
            continue
        mapped = call.get("mapped_argv", [])
        output = next(
            (
                Path(mapped[index + 1])
                for index, value in enumerate(mapped[:-1])
                if value == "--output"
            ),
            None,
        )
        if output is None or not output.is_file():
            continue
        parsed = _extract_candidate(
            output.read_text(encoding="utf-8", errors="replace")
        )
        if parsed is None:
            continue
        job_index = mapped.index("--job-id") if "--job-id" in mapped else -1
        work_index = mapped.index("--work-item") if "--work-item" in mapped else -1
        parsed.update(
            {
                "job_id": mapped[job_index + 1] if job_index >= 0 else None,
                "work_item": mapped[work_index + 1] if work_index >= 0 else None,
            }
        )
        candidates.append(parsed)
    safe_path = run_root / "safe-output-capture.jsonl"
    safe_outputs = [
        json.loads(line)
        for line in safe_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if safe_path.is_file() else []
    errors = [
        {"type": "helper_error", "message": call["error"], "argv": call.get("argv", [])}
        for call in helper_calls
        if call.get("error")
    ]
    if (
        "permission required" in agent_output.casefold()
        or "unavailable interactive approval" in agent_output.casefold()
        or "could not execute" in agent_output.casefold()
    ):
        errors.append(
            {
                "type": "environment_blocker",
                "code": "interactive_tool_approval_unavailable",
                "message": (
                    "Copilot CLI enterprise policy denied non-interactive "
                    "helper execution; no observer evidence was collected."
                ),
            }
        )
    incomplete_intents = {
        "missing_tool",
        "missing_data",
        "report_incomplete",
    }
    if any(item.get("tool") in incomplete_intents for item in safe_outputs):
        errors.append(
            {
                "type": "safe_output_incomplete",
                "message": "agent emitted an incomplete-scan safe-output intent",
            }
        )
    build_files = sorted(agent_root.glob("builds-*.json"))
    return {
        "schema_version": 2,
        "status": (
            "blocked"
            if any(error.get("type") == "environment_blocker" for error in errors)
            else "complete"
            if agent_status == 0 and not errors
            else "incomplete"
        ),
        "logical_now": "2026-10-05T12:03:42Z",
        "original_commit": ORIGINAL_COMMIT,
        "coverage": {
            "definitions_required": len(DEFINITIONS),
            "definitions_requested": len(build_files),
            "selected_builds": len(list(agent_root.glob("timeline-*.json"))),
            **_helper_coverage(helper_calls),
            "xharness_candidates": len(candidates),
        },
        "candidates": candidates,
        "errors": errors,
        "safe_output_intents": safe_outputs,
        "agent_inputs": {
            "helper_output_root": str(agent_root),
            "helper_status": agent_status,
        },
    }


def run_agent(
    run_root: Path,
    evidence_path: Path,
    model: str,
    *,
    fixture_path: Path | None = None,
    github_profile: str = "baseline",
    prepared_bundle: Path | None = None,
    hlx_package: Path | None = None,
    hlx_executable: Path | None = None,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    if fixture_path is None:
        fixture_name = "github-observer-empty.json" if prepared_bundle is not None else "github-mcp.json"
        fixture_path = ROOT / ".github/workflows/tests/observer_validation/fixtures" / fixture_name
    manifest = None
    if prepared_bundle is not None:
        github_profile = "issue-pr"
        if hlx_package is None or hlx_executable is None:
            raise ValueError("prepared agent validation requires --hlx and --hlx-package")
        manifest = _prepare_collected_inputs(run_root, prepared_bundle, hlx_executable)
    checkout, fidelity = _prepare_docker_runtime(
        run_root,
        fixture_path=fixture_path,
        github_profile=github_profile,
        revision=manifest["source"]["startingRevision"] if manifest else ORIGINAL_COMMIT,
        source_repository=(prepared_bundle / manifest["source"]["package"]["path"]) if manifest else None,
    )
    if manifest:
        workflow = (ROOT / ".github/workflows/runtime-failure-observer.agent.md").read_text()
        original = _materialize_analysis_agents(checkout, workflow)
        (run_root / "inputs").mkdir(exist_ok=True)
        (run_root / "inputs/analysis-workflow.md").write_text(workflow)
        config_path = run_root / "copilot-home/mcp-config.json"
        config = json.loads(config_path.read_text())
        config["mcpServers"]["hlx"] = {
            "type": "local", "command": "/opt/dotnet/dotnet",
            "args": ["/opt/hlx/HelixTool.dll", "mcp"],
            "env": {
                "HLX_EVAL_SNAPSHOT": "/tmp/gh-aw/agent/observer/hlx-snapshot",
                "DOTNET_ROOT": "/opt/dotnet", "DOTNET_NOLOGO": "1",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            },
            "tools": ["azdo_build", "azdo_timeline", "azdo_log", "azdo_search_log",
                      "helix_logs", "helix_search"],
        }
        config["mcpServers"]["safeoutputs"]["args"].extend([
            "--tools", "noop,missing_tool,missing_data,create_pull_request",
        ])
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        fidelity["image"] = _prepare_analysis_image(run_root)
        fidelity["offline_ci"] = {"snapshot": str(prepared_bundle), "live_fallback": False}
        fidelity["limitations"] = [
            "Pinned baseline Linux/Copilot image plus the pinned .NET runtime required by hlx.",
            "Strict fixture GitHub and captured safe outputs; not full gh-aw gateway/firewall orchestration.",
            "Recurrence re-derived by current collector over captured inputs; no live CI recollection.",
        ]
    else:
        original = (run_root / "inputs" / "observer-prompt.md").read_text(encoding="utf-8")
    (run_root / "agent-prompt.txt").write_text(original, encoding="utf-8")
    copilot_args = [
        f"--prompt={original}",
        "--model",
        model,
        "--silent",
        "--disable-builtin-mcps",
        "--available-tools",
        "bash",
        "github",
        "safeoutputs",
        "--excluded-tools",
        "edit",
        "apply_patch",
        "write_file",
        "view",
        "glob",
        "rg",
        "--allow-tool",
        "github(search_issues)",
        "--allow-tool",
        "github(search_pull_requests)",
        "--allow-tool",
        "github(search_code)",
        "--allow-tool",
        "github(list_issues)",
        "--allow-tool",
        "github(list_pull_requests)",
        "--allow-tool",
        "github(issue_read)",
        "--allow-tool",
        "github(pull_request_read)",
        "--allow-tool",
        "github(get_file_contents)",
        "--allow-tool",
        "github(get_commit)",
        "--allow-tool",
        "safeoutputs",
        "--allow-tool",
        "shell(runtime-failure-observer-http:*)",
        "--allow-tool",
        "shell(awk)",
        "--allow-tool",
        "shell(basename)",
        "--allow-tool",
        "shell(cat)",
        "--allow-tool",
        "shell(command)",
        "--allow-tool",
        "shell(cut)",
        "--allow-tool",
        "shell(date)",
        "--allow-tool",
        "shell(dirname)",
        "--allow-tool",
        "shell(echo)",
        "--allow-tool",
        "shell(env)",
        "--allow-tool",
        "shell(find)",
        "--allow-tool",
        "shell(file)",
        "--allow-tool",
        "shell(grep)",
        "--allow-tool",
        "shell(head)",
        "--allow-tool",
        "shell(jq)",
        "--allow-tool",
        "shell(ls)",
        "--allow-tool",
        "shell(mkdir)",
        "--allow-tool",
        "shell(printf)",
        "--allow-tool",
        "shell(sed)",
        "--allow-tool",
        "shell(sort)",
        "--allow-tool",
        "shell(tail)",
        "--allow-tool",
        "shell(tee)",
        "--allow-tool",
        "shell(test)",
        "--allow-tool",
        "shell(tr)",
        "--allow-tool",
        "shell(uniq)",
        "--allow-tool",
        "shell(wc)",
        "--allow-tool",
        "shell(xargs)",
        "--allow-tool",
        "shell(:)",
        "--allow-tool",
        "shell(git diff:*)",
        "--allow-tool",
        "shell(git log:*)",
        "--allow-tool",
        "shell(git rev-parse:*)",
        "--allow-tool",
        "shell(git show:*)",
        "--allow-tool",
        "shell(git status)",
    ]
    if manifest:
        excluded = copilot_args.index("--excluded-tools")
        allow = copilot_args.index("--allow-tool", excluded)
        del copilot_args[excluded:allow]
        available = copilot_args.index("--available-tools") + 1
        copilot_args[available:available] = [
            "hlx", "task", "read_agent", "view", "glob", "rg", "edit", "create",
            "apply_patch", "write_file",
        ]
        copilot_args.extend(["--allow-tool", "hlx", "--allow-tool", "task",
                             "--allow-tool", "read_agent", "--allow-tool", "read",
                             "--allow-tool", "write",
                             "--allow-tool", "shell(git:*)",
                             "--deny-tool", "shell(git push)",
                             "--deny-tool", "shell(git fetch)",
                             "--deny-tool", "shell(git pull)",
                             "--allow-tool", "shell(while)",
                             "--allow-tool", "shell(read)",
                             "--allow-tool", "shell(if)"])
        helper_permission = copilot_args.index("shell(runtime-failure-observer-http:*)")
        del copilot_args[helper_permission - 1:helper_permission + 1]
    copilot_args.extend(
        ["--add-dir", "/workspace", "--add-dir", "/workspace/xharness-checkout", "--add-dir", "/tmp"]
    )
    (run_root / "home").mkdir(parents=True, exist_ok=True)
    (run_root / "copilot-logs").mkdir(parents=True, exist_ok=True)
    docker_env = {
        "COPILOT_HOME": "/workspace/copilot-home",
        "COPILOT_AUTO_UPDATE": "false",
        "GITHUB_WORKSPACE": "/workspace/xharness-checkout",
        "GITHUB_REPOSITORY": "dotnet/xharness",
        "RUNNER_TEMP": "/workspace/runner-temp",
        "GH_AW_SAFE_OUTPUTS": "/workspace/safe-output-capture.jsonl",
        "OBSERVER_AGENT_ROOT": "/workspace/agent-state",
        "OBSERVER_HELPER_CALLS": "/workspace/helper-invocations.jsonl",
        "OBSERVER_BLOCKED_COMMANDS": "/workspace/blocked-commands.log",
        "TMPDIR": "/workspace/native-tmp",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/workspace/home",
        "PATH": "/workspace/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    docker_command = [
        "docker",
        "run",
        "--rm",
        "--cidfile",
        str(run_root / "container-id"),
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,size=512m",
        "--security-opt",
        "no-new-privileges",
        "--network",
        "bridge",
        "--workdir",
        "/workspace/xharness-checkout",
        "--volume",
        f"{run_root}:/workspace:rw",
        "--volume",
        f"{run_root / 'agent-state'}:/tmp/gh-aw/agent:rw",
    ]
    for name, value in docker_env.items():
        docker_command.extend(["--env", f"{name}={value}"])
    if "GH_TOKEN" in os.environ:
        docker_command.extend(["--env", "GH_TOKEN"])
    if manifest:
        for relative in ("hlx-snapshot", "source", "version-details"):
            directory = prepared_bundle / relative
            if directory.is_dir():
                docker_command.extend(["--volume", f"{directory.resolve()}:/tmp/gh-aw/agent/observer/{relative}:ro"])
        docker_command.extend(["--volume", f"{hlx_package.resolve()}:/opt/hlx:ro"])
    command = docker_command + [
        "--entrypoint", "copilot", fidelity["image"], *copilot_args,
        "--log-level", "all", "--log-dir", "/workspace/copilot-logs",
    ]
    try:
        completed = subprocess.run(
            command, cwd=run_root, env=os.environ.copy(), text=True,
            capture_output=True, check=False, timeout=3600,
        )
    except subprocess.TimeoutExpired as error:
        container_id = (run_root / "container-id").read_text().strip()
        subprocess.run(
            ["docker", "stop", "--time", "10", container_id],
            check=True, capture_output=True, timeout=30,
        )
        def output_text(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        completed = subprocess.CompletedProcess(
            command, 124, output_text(error.stdout),
            output_text(error.stderr) + "\nObserver agent exceeded the 3600-second deadline.",
        )
    (run_root / "agent.stdout").write_text(completed.stdout, encoding="utf-8")
    (run_root / "agent.stderr").write_text(completed.stderr, encoding="utf-8")
    result = {
        "exit_code": completed.returncode,
        "model": model,
        "stdout": completed.stdout,
        "prompt_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "fidelity": fidelity,
    }
    evidence = (
        _summarize_prepared_run(run_root, manifest, completed.returncode, fidelity)
        if manifest else
        _summarize_actual_run(run_root, completed.returncode, completed.stdout + "\n" + completed.stderr)
    )
    evidence["agent"] = {
        "exit_code": completed.returncode,
        "model": model,
        "stdout_path": str(run_root / "agent.stdout"),
        "stderr_path": str(run_root / "agent.stderr"),
        "fidelity": fidelity,
    }
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_root / "agent-result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _evidence_path(value: Path) -> Path:
    return value / "evidence.json" if value.is_dir() else value


def compare_runs(left: Path, right: Path, output: Path) -> dict[str, Any]:
    left_evidence = json.loads(_evidence_path(left).read_text(encoding="utf-8"))
    right_evidence = json.loads(_evidence_path(right).read_text(encoding="utf-8"))
    left_candidates = {
        (item.get("definition"), item.get("build_id"), item.get("job_id"), item.get("work_item"), item.get("exit_code"), item.get("signature"))
        for item in left_evidence.get("candidates", [])
    }
    right_candidates = {
        (item.get("definition"), item.get("build_id"), item.get("job_id"), item.get("work_item"), item.get("exit_code"), item.get("signature"))
        for item in right_evidence.get("candidates", [])
    }
    def intent_summary(evidence: dict[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in evidence.get("safe_output_intents", []):
            kind = item.get("tool", item.get("type", "unknown"))
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    left_manifest = (
        json.loads((left / "run-manifest.json").read_text(encoding="utf-8"))
        if left.is_dir() and (left / "run-manifest.json").is_file()
        else {}
    )
    right_manifest = (
        json.loads((right / "run-manifest.json").read_text(encoding="utf-8"))
        if right.is_dir() and (right / "run-manifest.json").is_file()
        else {}
    )
    left_fidelity = left_evidence.get("agent", {}).get("fidelity", left_manifest.get("fidelity"))
    right_fidelity = right_evidence.get("agent", {}).get("fidelity", right_manifest.get("fidelity"))
    coverage = {
        "left": dict(left_evidence.get("coverage", {})),
        "right": dict(right_evidence.get("coverage", {})),
    }
    for side, path in (("left", left), ("right", right)):
        root = path if path.is_dir() else path.parent
        calls = root / "helper-invocations.jsonl"
        if calls.is_file():
            records = [json.loads(line) for line in calls.read_text().splitlines() if line]
            for key, value in _helper_coverage(records).items():
                if key in coverage[side] and coverage[side][key] != value:
                    coverage[side][f"recorded_{key}"] = coverage[side][key]
                coverage[side][key] = value
    comparison = {
        "schema_version": 2,
        "left": str(left),
        "right": str(right),
        "status": {
            "left": left_evidence.get("status", "complete"),
            "right": right_evidence.get("status", "complete"),
            "same": left_evidence.get("status", "complete") == right_evidence.get("status", "complete"),
        },
        "coverage": coverage,
        "input_traceability": {
            "left": left_evidence.get("original_inputs", {}),
            "right": right_evidence.get("original_inputs", {}),
            "same": left_evidence.get("original_inputs", {})
            == right_evidence.get("original_inputs", {}),
        },
        "input_drift": {
            "original_inputs_same": left_evidence.get("original_inputs", {})
            == right_evidence.get("original_inputs", {}),
            "original_commits_same": left_evidence.get("original_commit")
            == right_evidence.get("original_commit"),
        },
        "outcome_intents": {
            "left": intent_summary(left_evidence),
            "right": intent_summary(right_evidence),
        },
        "recurrence": {
            "left": left_evidence.get("recurrence", []),
            "right": right_evidence.get("recurrence", []),
        },
        "environment_fidelity": {
            "left": left_fidelity,
            "right": right_fidelity,
            "same_mode": (
                (left_fidelity or {}).get("mode")
                == (right_fidelity or {}).get("mode")
            ),
        },
        "candidate_diff": {
            "only_left": [list(item) for item in sorted(left_candidates - right_candidates, key=str)],
            "only_right": [list(item) for item in sorted(right_candidates - left_candidates, key=str)],
        },
        "errors": {
            "left": left_evidence.get("errors", []),
            "right": right_evidence.get("errors", []),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("actual", "prepared", "compare"),
        required=True,
    )
    parser.add_argument("--github-fixture", type=Path)
    parser.add_argument("--github-profile", choices=("baseline", "issue-pr"), default="baseline")
    parser.add_argument("--left", type=Path)
    parser.add_argument("--right", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/observer-validation/runs"))
    parser.add_argument("--timestamp", default="2026-10-05T12:03:42Z")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--hlx-package", type=Path, help="hlx tool's tools/net10.0/any directory")
    parser.add_argument("--hlx", type=Path, help="host hlx executable for offline re-indexing")
    args = parser.parse_args(argv)
    if args.mode == "compare":
        if args.left is None or args.right is None:
            parser.error("--left and --right are required in compare mode")
        output = args.output if args.output.suffix == ".json" else args.output / "comparison.json"
        comparison = compare_runs(args.left, args.right, output)
        print(output)
        print(json.dumps(comparison["candidate_diff"], sort_keys=True))
        return 0
    logical_now = _parse_time(args.timestamp)
    run_root = args.output / logical_now.strftime("%Y%m%dT%H%M%SZ")
    if args.mode == "prepared":
        if args.bundle is None or args.hlx_package is None or args.hlx is None:
            parser.error("--bundle, --hlx, and --hlx-package are required in prepared mode")
        run_root.mkdir(parents=True, exist_ok=False)
        run_agent(
            run_root, run_root / "evidence.json", args.model,
            fixture_path=args.github_fixture, github_profile="issue-pr",
            prepared_bundle=args.bundle.resolve(),
            hlx_package=args.hlx_package.resolve(),
            hlx_executable=args.hlx.resolve(),
        )
        evidence = json.loads((run_root / "evidence.json").read_text())
        print(run_root / "evidence.json")
        return 0 if evidence["status"] == "complete" else 2
    run_root.mkdir(parents=True, exist_ok=False)
    input_manifest = freeze_original_inputs(run_root)
    result = run_agent(
        run_root,
        run_root / "evidence.json",
        args.model,
        fixture_path=args.github_fixture,
        github_profile=args.github_profile,
    )
    evidence = json.loads((run_root / "evidence.json").read_text(encoding="utf-8"))
    status = evidence.get("status", "incomplete")
    evidence["execution_kind"] = "actual-original-agent-control-flow"
    evidence["github_profile"] = args.github_profile
    evidence["agent_process_exit_code"] = result["exit_code"]
    evidence["original_commit"] = ORIGINAL_COMMIT
    evidence["original_inputs"] = input_manifest
    (run_root / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "mode": args.mode,
        "runtime": "docker",
        "logical_now": args.timestamp,
        "model": args.model,
        "original_commit": ORIGINAL_COMMIT,
        "original_inputs": input_manifest,
        "writes": [],
        "safe_output_capture": "local MCP capture",
        "status": status,
        "fidelity": evidence["agent"]["fidelity"],
    }
    (run_root / "run-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(run_root)
    print(json.dumps(evidence.get("coverage", {}), sort_keys=True))
    return 0 if status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
