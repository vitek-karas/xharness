import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import local_observer_harness as harness
from local_observer_harness import (
    FixtureGitHubMCP,
    _extract_candidate,
    _summarize_actual_run,
    _materialize_analysis_agents,
    _summarize_prepared_run,
    compare_runs,
)


class CandidateTests(unittest.TestCase):
    def test_extracts_target_and_sanitizes_evidence(self):
        candidate = _extract_candidate(
            "Running command: /agent/abc xharness apple test\n"
            "XHarness version 1.0+4f2266d4c147bd8af3fe4dff129f725ddb9dce57\n"
            "device /private/machine/secret unavailable\n"
            "device not found\n"
            "Exit code: 81\n"
        )
        self.assertEqual(candidate["exit_code"], 81)
        self.assertEqual(candidate["signature"], "device <path> unavailable")
        self.assertNotIn("/private", candidate["command"])
        self.assertEqual(candidate["version_sha"], "4f2266d4c147bd8af3fe4dff129f725ddb9dce57")

    def test_non_xharness_console_is_ignored(self):
        self.assertIsNone(_extract_candidate("Running command: dotnet test\nExit code: 1\n"))


class FixtureMcpTests(unittest.TestCase):
    def test_pinned_read_schema_uses_method_and_pull_number_casing(self):
        issue = FixtureGitHubMCP.TOOL_SCHEMAS["issue_read"]
        pull = FixtureGitHubMCP.TOOL_SCHEMAS["pull_request_read"]
        self.assertIn("method", issue["required"])
        self.assertIn("method", pull["required"])
        self.assertIn("pullNumber", pull["required"])
        self.assertNotIn("pull_number", pull["properties"])

    def test_pagination_followup_and_unknown_errors_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls.jsonl"
            query = {"query": "repo:dotnet/xharness", "page": 1, "perPage": 1}
            second = {**query, "page": 2}
            followup = {"method": "get", "owner": "dotnet", "repo": "xharness", "pullNumber": 10}
            server = FixtureGitHubMCP({"requests": [
                {"tool": "search_pull_requests", "arguments": query, "responses": [
                    {"items": [{"number": 10}], "total_count": 2, "incomplete_results": False}]},
                {"tool": "search_pull_requests", "arguments": second, "responses": [
                    {"items": [{"number": 11}], "total_count": 2, "incomplete_results": False}]},
                {"tool": "pull_request_read", "arguments": followup, "responses": [
                    {"number": 10, "state": "closed", "merged": True}]},
            ]}, calls_path=calls)
            self.assertEqual(server.call("search_pull_requests", query)["items"][0]["number"], 10)
            self.assertEqual(server.call("search_pull_requests", second)["items"][0]["number"], 11)
            self.assertTrue(server.call("pull_request_read", followup)["merged"])
            with self.assertRaisesRegex(RuntimeError, "fixture miss"):
                server.call("search_pull_requests", {**query, "page": 3})
            recorded = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertIn("error", recorded[-1])

    def test_declares_and_records_required_read_tools(self):
        server = FixtureGitHubMCP(
            {
                "requests": [
                    {
                        "tool": "search_issues",
                        "arguments": {
                            "owner": "dotnet",
                            "repo": "xharness",
                            "query": "repo:dotnet/xharness",
                            "page": 1,
                            "perPage": 30,
                        },
                        "responses": [{"items": []}, {"items": ["second-call"]}],
                    }
                ]
            }
        )
        names = {tool["name"] for tool in server.list_tools()}
        self.assertIn("search_issues", names)
        self.assertIn("search_pull_requests", names)
        arguments = {
            "owner": "dotnet",
            "repo": "xharness",
            "query": "repo:dotnet/xharness",
            "page": 1,
            "perPage": 30,
        }
        self.assertEqual(server.call("search_issues", arguments)["items"], [])
        self.assertEqual(
            server.call("search_issues", arguments)["items"], ["second-call"]
        )
        self.assertEqual(server.calls[0]["arguments"]["owner"], "dotnet")
        self.assertNotIn("per_page", server.list_tools()[0]["inputSchema"]["properties"])

    def test_fixture_miss_is_explicit_and_never_empty(self):
        server = FixtureGitHubMCP(
            {
                "requests": [
                    {
                        "tool": "search_issues",
                        "arguments": {"query": "known"},
                        "responses": [{"items": []}],
                    }
                ]
            }
        )
        with self.assertRaisesRegex(RuntimeError, "fixture miss"):
            server.call("search_issues", {"query": "unknown"})

    def test_recorded_protocol_error_is_explicit(self):
        server = FixtureGitHubMCP(
            {
                "requests": [
                    {
                        "tool": "issue_read",
                        "arguments": {"owner": "dotnet", "repo": "xharness", "issue_number": 1},
                        "responses": [{"error": {"code": "not_found", "message": "missing"}}],
                    }
                ]
            }
        )
        with self.assertRaisesRegex(RuntimeError, "not_found"):
            server.call(
                "issue_read",
                {"owner": "dotnet", "repo": "xharness", "issue_number": 1},
            )


class SafeOutputTests(unittest.TestCase):
    def test_captures_intent_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe-outputs.jsonl"
            server_type = runpy.run_path(str(HERE / "fixture-github-mcp.py"))["SafeOutputMCP"]
            server = server_type(path, allowed_tools={"noop"})
            server.call("noop", {"message": "fixture complete"})
            self.assertEqual(json.loads(path.read_text())["tool"], "noop")
            with self.assertRaisesRegex(RuntimeError, "not available"):
                server.call("add_comment", {})


class ActualRunTests(unittest.TestCase):
    def test_default_fixtures_match_each_execution_mode(self):
        manifest = {"source": {"startingRevision": "fixture", "package": {"path": "source/xharness.git"}}}
        for prepared in (False, True):
            with self.subTest(prepared=prepared), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(harness, "_prepare_collected_inputs", return_value=manifest), \
                     patch.object(harness, "_prepare_docker_runtime", side_effect=RuntimeError("stop before Docker")) as setup:
                    with self.assertRaisesRegex(RuntimeError, "stop before Docker"):
                        harness.run_agent(
                            root, root / "evidence.json", "gpt-6.1-sol",
                            prepared_bundle=root / "bundle" if prepared else None,
                            hlx_package=root / "hlx",
                            hlx_executable=root / "hlx/hlx",
                        )
                fixture = setup.call_args.kwargs["fixture_path"]
                expected = "github-observer-empty.json" if prepared else "github-mcp.json"
                self.assertEqual(fixture.name, expected)
                self.assertEqual(setup.call_args.kwargs["github_profile"], "issue-pr" if prepared else "baseline")
                if prepared:
                    server = FixtureGitHubMCP(json.loads(fixture.read_text()))
                    for tool, kind in (("search_issues", "issue"), ("search_pull_requests", "pr")):
                        response = server.call(tool, {
                            "owner": "dotnet", "repo": "xharness",
                            "query": f'repo:dotnet/xharness is:{kind} in:title "[runtime-observer]"',
                            "page": 1, "perPage": 100,
                        })
                        self.assertEqual(response["total_count"], 0)
                        self.assertFalse(response["incomplete_results"])

    def test_explicit_fixture_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "custom.json"
            with patch.object(harness, "_prepare_docker_runtime", side_effect=RuntimeError("stop before Docker")) as setup:
                with self.assertRaisesRegex(RuntimeError, "stop before Docker"):
                    harness.run_agent(root, root / "evidence.json", "gpt-6.1-sol", fixture_path=fixture)
            self.assertEqual(setup.call_args.kwargs["fixture_path"], fixture)

    def test_docker_execution_preserves_mounts_tools_and_recording(self):
        for prepared in (False, True):
            with self.subTest(prepared=prepared), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                checkout = root / "xharness-checkout"
                checkout.mkdir()
                bundle = root / "bundle"
                for name in ("hlx-snapshot", "source", "version-details"):
                    (bundle / name).mkdir(parents=True)
                (root / "inputs").mkdir()
                (root / "inputs/observer-prompt.md").write_text("Original observer prompt")
                observer = root / "agent-state/observer"
                observer.mkdir(parents=True)
                (observer / "failure-index.json").write_text('{"candidates":[]}')
                (root / "safe-output-capture.jsonl").write_text(
                    '{"tool":"noop","arguments":{"message":"complete"}}\n'
                )
                manifest = {
                    "asOf": "2026-10-05T12:03:42Z",
                    "counts": {"definitions": 9, "selectedBuilds": 1, "helixJobs": 1},
                    "source": {"startingRevision": "fixture", "package": {"path": "source/xharness.git"}},
                }
                with patch.object(harness, "_prepare_isolated_checkout", return_value=checkout), \
                     patch.object(harness, "_prepare_collected_inputs", return_value=manifest), \
                     patch.object(harness, "_prepare_analysis_image", return_value="prepared-image"), \
                     patch.object(harness.subprocess, "run", return_value=subprocess.CompletedProcess(
                         [], 0, "agent output", "",
                     )) as run:
                    result = harness.run_agent(
                        root, root / "evidence.json", "gpt-6.1-sol",
                        prepared_bundle=bundle if prepared else None,
                        hlx_package=root / "hlx", hlx_executable=root / "hlx/hlx",
                    )
                command = run.call_args.args[0]
                self.assertEqual(command[:2], ["docker", "run"])
                self.assertIn("--read-only", command)
                self.assertIn(f"{root}/agent-state:/tmp/gh-aw/agent:rw", command)
                self.assertEqual(run.call_args.kwargs["timeout"], 3600)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual((root / "agent.stdout").read_text(), "agent output")
                self.assertEqual(json.loads((root / "evidence.json").read_text())["status"], "complete")
                config = json.loads((root / "copilot-home/mcp-config.json").read_text())
                if prepared:
                    self.assertNotIn("--excluded-tools", command)
                    self.assertNotIn("shell(runtime-failure-observer-http:*)", command)
                    self.assertIn("prepared-image", command)
                    denied = {command[index + 1] for index, value in enumerate(command) if value == "--deny-tool"}
                    self.assertEqual(denied, {"shell(git push)", "shell(git fetch)", "shell(git pull)"})
                    for name in ("hlx-snapshot", "source", "version-details"):
                        self.assertIn(f"{bundle}/{name}:/tmp/gh-aw/agent/observer/{name}:ro", command)
                    self.assertEqual(config["mcpServers"]["hlx"]["env"]["HLX_EVAL_SNAPSHOT"],
                                     "/tmp/gh-aw/agent/observer/hlx-snapshot")
                    self.assertEqual(config["mcpServers"]["github"]["args"][-1],
                                     "search_issues,search_pull_requests,issue_read,pull_request_read")
                    self.assertEqual(config["mcpServers"]["safeoutputs"]["args"][-1],
                                     "noop,missing_tool,missing_data,create_pull_request")
                else:
                    self.assertIn("--excluded-tools", command)
                    self.assertIn("shell(runtime-failure-observer-http:*)", command)
                    self.assertNotIn("hlx", config["mcpServers"])

    def test_obsolete_modes_and_native_runtime_are_rejected(self):
        cases = [("--mode", mode) for mode in ("fixture", "live", "transport")]
        cases.append(("--mode", "actual", "--runtime", "native"))
        for arguments in cases:
            with self.subTest(arguments=arguments), patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    harness.main(list(arguments))
                self.assertEqual(raised.exception.code, 2)

    def test_counts_helper_invocations_not_start_and_completion_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = {"argv": ["azdo-builds", "--definition", "154"]}
            end = {**start, "exit_code": 0}
            (root / "helper-invocations.jsonl").write_text(
                json.dumps(start) + "\n" + json.dumps(end) + "\n"
            )
            evidence = _summarize_actual_run(root, 0)
            self.assertEqual(evidence["coverage"]["helper_requests"], 1)
            self.assertEqual(evidence["coverage"]["helper_request_records"], 2)

    def test_materializes_native_analysis_agents_and_separates_main_prompt(self):
        source = (HERE.parents[1] / "runtime-failure-observer.agent.md").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = _materialize_analysis_agents(root, source)
            self.assertIn("# Runtime Failure Observer", prompt)
            self.assertNotIn("## agent:", prompt)
            for name in ("runtime-candidate-analyst", "xharness-fix-implementer"):
                agent = root / ".github/agents" / f"{name}.agent.md"
                self.assertTrue(agent.is_file())
                self.assertIn("model: gpt-6.1-sol", agent.read_text())

    def test_prepared_run_without_safe_output_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "agent-state/observer"
            inputs.mkdir(parents=True)
            (inputs / "failure-index.json").write_text('{"candidates":[]}')
            manifest = {"asOf": "2026-10-05T12:03:42Z", "counts": {
                "definitions": 9, "selectedBuilds": 0, "helixJobs": 0},
                "source": {"startingRevision": "fixture"}}
            result = _summarize_prepared_run(root, manifest, 0, {})
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["errors"], [{"type": "safe_output_missing"}])

    def test_prepared_safe_outputs_respect_cap_and_exclusivity(self):
        cases = (
            (["noop"], "complete"),
            (["create_pull_request"] * 2, "complete"),
            (["create_pull_request"] * 3, "incomplete"),
            (["noop", "create_pull_request"], "incomplete"),
            (["add_comment"], "incomplete"),
        )
        for tools, expected in cases:
            with self.subTest(tools=tools), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inputs = root / "agent-state/observer"
                inputs.mkdir(parents=True)
                (inputs / "failure-index.json").write_text('{"candidates":[]}')
                (root / "safe-output-capture.jsonl").write_text("".join(
                    json.dumps({"tool": tool, "arguments": {}}) + "\n"
                    for tool in tools
                ))
                manifest = {"asOf": "2026-10-05T12:03:42Z", "counts": {
                    "definitions": 9, "selectedBuilds": 0, "helixJobs": 0},
                    "source": {"startingRevision": "fixture"}}
                evidence = _summarize_prepared_run(root, manifest, 0, {})
                self.assertEqual(evidence["status"], expected)

    def test_permission_blocker_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = _summarize_actual_run(
                Path(directory),
                0,
                "required helper could not execute because permission required",
            )
            self.assertEqual(evidence["status"], "blocked")
            self.assertEqual(
                evidence["errors"][0]["code"],
                "interactive_tool_approval_unavailable",
            )
            self.assertEqual(evidence["coverage"]["helper_requests"], 0)


class ComparisonTests(unittest.TestCase):
    def test_frozen_helper_counts_are_corrected_without_rewriting_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left", root / "right"
            left.mkdir()
            right.mkdir()
            original = '{"coverage":{"helper_requests":4,"helix_work_item_inventories":0}}'
            (left / "evidence.json").write_text(original)
            (right / "evidence.json").write_text("{}")
            (left / "helper-invocations.jsonl").write_text(
                '{"argv":["azdo-builds"]}\n{"argv":["azdo-builds"],"exit_code":0}\n'
                '{"argv":["helix-work-items","--job-id","fixture-job"]}\n'
                '{"argv":["helix-work-items","--job-id","fixture-job"],"exit_code":0}\n'
            )
            result = compare_runs(left, right, root / "comparison.json")
            self.assertEqual(result["coverage"]["left"]["helper_requests"], 2)
            self.assertEqual(result["coverage"]["left"]["helper_request_records"], 4)
            self.assertEqual(result["coverage"]["left"]["recorded_helper_requests"], 4)
            self.assertEqual(result["coverage"]["left"]["helix_work_item_inventories"], 1)
            self.assertEqual(result["coverage"]["left"]["recorded_helix_work_item_inventories"], 0)
            self.assertEqual((left / "evidence.json").read_text(), original)

    def test_reports_candidate_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.json"
            right = root / "right.json"
            left.write_text(json.dumps({"candidates": [{"signature": "left"}]}))
            right.write_text(json.dumps({"candidates": [{"signature": "right"}]}))
            output = root / "comparison.json"
            result = compare_runs(left, right, output)
            self.assertEqual(result["candidate_diff"]["only_left"], [[None, None, None, None, None, "left"]])
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
