import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).parents[1]
REPO = WORKFLOWS.parents[1]
SOURCE = WORKFLOWS / "runtime-failure-observer.agent.md"
LOCK = WORKFLOWS / "runtime-failure-observer.agent.lock.yml"
HANDOFF = WORKFLOWS / "runtime-failure-observer-handoff.py"
TEST_TEMP_ROOT = REPO / "artifacts" / "observer-validation" / "workflow-handoff-tests"


class RuntimeFailureObserverWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text()
        cls.lock = LOCK.read_text()
        cls.metadata = json.loads(cls.lock.splitlines()[0].removeprefix("# gh-aw-metadata: "))
        cls.manifest = json.loads(cls.lock.splitlines()[1].removeprefix("# gh-aw-manifest: "))

    def run_handoff(
        self,
        manifest,
        failure_index,
        *,
        collector_exit=0,
        snapshot=True,
        missing=(),
    ):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            output = Path(temporary) / "observer"
            output.mkdir()
            manifest_text = (
                manifest if isinstance(manifest, str) else json.dumps(manifest)
            )
            index_text = (
                failure_index
                if isinstance(failure_index, str)
                else json.dumps(failure_index)
            )
            files = {
                "observer-manifest.json": manifest_text,
                "failure-index.json": index_text,
                "analysis-inputs.json": "{}",
                "filed.tsv": "",
            }
            for name, content in files.items():
                if name not in missing:
                    (output / name).write_text(content)
            if snapshot:
                (output / "hlx-snapshot").mkdir()
            safe_outputs = Path(temporary) / "safe-outputs.jsonl"
            result = subprocess.run(
                [sys.executable, str(HANDOFF)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "OBSERVER_OUTPUT": str(output),
                    "OBSERVER_COLLECTOR_EXIT_CODE": str(collector_exit),
                    "GH_AW_SAFE_OUTPUTS": str(safe_outputs),
                },
            )
            outputs = (
                [
                    json.loads(line)
                    for line in safe_outputs.read_text().splitlines()
                    if line
                ]
                if safe_outputs.is_file()
                else []
            )
            return result, outputs

    def run_raw_cache_selection(self, *, snapshot, raw_cache, validated=True):
        marker = "      python3 - <<'PY_RAW_CACHE'\n"
        script = textwrap.dedent(
            self.source.split(marker, 1)[1].split("\n      PY_RAW_CACHE", 1)[0]
        )
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            output = Path(temporary) / "observer"
            output.mkdir()
            if snapshot:
                (output / "hlx-snapshot").mkdir()
            if raw_cache:
                (output / "xdg-cache" / "hlx" / "public").mkdir(parents=True)
            (output / "observer-manifest.json").write_text(
                json.dumps({"snapshot": {"validation": {"valid": validated}}})
            )
            github_output = Path(temporary) / "github-output"
            result = subprocess.run(
                [sys.executable, "-c", script],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "OBSERVER_OUTPUT": str(output),
                    "GITHUB_OUTPUT": str(github_output),
                },
            )
            values = dict(
                line.split("=", 1)
                for line in github_output.read_text().splitlines()
                if "=" in line
            )
            return result, values

    @staticmethod
    def complete_manifest(*, empty):
        return {
            "schemaVersion": 1,
            "kind": "xharness.runtime-failure-observer.collect",
            "status": "complete",
            "complete": True,
            "empty": empty,
            "errors": [],
        }

    def test_workflow_invokes_collector_and_handoff_adapter(self):
        self.assertIn("observer_output=/tmp/gh-aw/agent/observer", self.source)
        self.assertIn('if [ -e "$observer_output" ]; then', self.source)
        self.assertIn('.github/workflows/runtime-failure-observer-collect', self.source)
        self.assertIn('--xharness-repo "${GITHUB_WORKSPACE}"', self.source)
        self.assertIn("python3 .github/workflows/runtime-failure-observer-handoff.py", self.source)
        self.assertNotIn("runtime-failure-observer-handoff.py --raw-cache", self.source)
        self.assertIn("OBSERVER_COLLECTOR_EXIT_CODE", self.source)

    def test_preserves_scheduled_and_manual_triggers(self):
        self.assertIn("schedule: every 12h", self.source)
        self.assertRegex(self.lock, r'cron: "[0-5]?\d \*/12 \* \* \*"')
        self.assertIn("workflow_dispatch:", self.source)

    def test_complete_empty_handoff_requires_snapshot_and_emits_noop(self):
        result, outputs = self.run_handoff(
            self.complete_manifest(empty=True),
            {"candidates": []},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["type"] for item in outputs], ["noop"])

        result, outputs = self.run_handoff(
            self.complete_manifest(empty=True),
            {"candidates": []},
            snapshot=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([item["type"] for item in outputs], ["missing_data"])

    def test_incomplete_and_malformed_handoffs_emit_missing_data(self):
        cases = (
            (
                {
                    "complete": False,
                    "empty": False,
                    "errors": [42],
                },
                {"candidates": []},
                2,
            ),
            ([], {"candidates": []}, 0),
            (self.complete_manifest(empty=False), [], 0),
            ("not JSON", {"candidates": []}, 0),
            (self.complete_manifest(empty=False), "not JSON", 0),
            (self.complete_manifest(empty=False), {"candidates": []}, 0),
            (self.complete_manifest(empty=True), {"candidates": []}, 1),
            (self.complete_manifest(empty=True), {}, 0),
            (self.complete_manifest(empty=True), {"candidates": None}, 0),
        )
        for manifest, failure_index, collector_exit in cases:
            with self.subTest(manifest=manifest, failure_index=failure_index):
                result, outputs = self.run_handoff(
                    manifest,
                    failure_index,
                    collector_exit=collector_exit,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    [item["type"] for item in outputs],
                    ["missing_data"],
                )
                self.assertNotIn("AttributeError", result.stderr)

    def test_incomplete_handoff_preserves_bounded_error_details(self):
        manifest = {
            **self.complete_manifest(empty=False),
            "complete": False,
            "errors": [{
                "code": "required_console_missing",
                "message": "Required console unavailable: " + "x" * 300,
                "scope": {"buildId": 123, "workItem": "y" * 300},
            }],
        }
        result, outputs = self.run_handoff(manifest, {"candidates": []}, collector_exit=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["type"], "missing_data")
        self.assertTrue(outputs[0]["reason"].startswith("required_console_missing:"))
        self.assertIn('"buildId":123', outputs[0]["context"])
        self.assertLessEqual(len(outputs[0]["reason"]), 256)
        self.assertLessEqual(len(outputs[0]["context"]), 256)

    def test_required_files_and_manifest_contract_are_validated(self):
        for filename in ("observer-manifest.json", "failure-index.json", "analysis-inputs.json", "filed.tsv"):
            with self.subTest(missing=filename):
                result, outputs = self.run_handoff(
                    self.complete_manifest(empty=True), {"candidates": []}, missing=(filename,),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([item["type"] for item in outputs], ["missing_data"])
        for field, value in (("schemaVersion", 2), ("kind", "other"), ("status", "incomplete"), ("errors", [{"code": "missing"}])):
            with self.subTest(field=field):
                manifest = {**self.complete_manifest(empty=True), field: value}
                result, outputs = self.run_handoff(manifest, {"candidates": []})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([item["type"] for item in outputs], ["missing_data"])

    def test_complete_nonempty_handoff_starts_without_safe_output(self):
        result, outputs = self.run_handoff(
            self.complete_manifest(empty=False),
            {"candidates": [{"id": "candidate-1"}]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, [])

    def test_raw_cache_is_selected_only_when_failed_export_has_no_snapshot(self):
        cases = (
            (False, True, "true"),
            (True, True, "false"),
            (False, False, "false"),
        )
        for snapshot, raw_cache, expected in cases:
            with self.subTest(snapshot=snapshot, raw_cache=raw_cache):
                result, values = self.run_raw_cache_selection(
                    snapshot=snapshot,
                    raw_cache=raw_cache,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(values["upload"], expected)
        result, values = self.run_raw_cache_selection(
            snapshot=True, raw_cache=True, validated=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["upload"], "true")

    def test_index_errors_cannot_become_complete(self):
        result, outputs = self.run_handoff(
            self.complete_manifest(empty=False),
            {"candidates": [{"id": "candidate-1"}], "errors": [{"code": "missing"}]},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([item["type"] for item in outputs], ["missing_data"])

    def test_uses_offline_hlx_snapshot_without_old_http_helper(self):
        self.assertIn("HLX_EVAL_SNAPSHOT", self.source)
        self.assertRegex(
            self.source,
            r"\$\{RUNNER_TEMP\}/gh-aw/observer-hlx:"
            r"/opt/runtime-observer/hlx:ro",
        )
        self.assertIn(
            "/tmp/gh-aw/agent/observer/hlx-snapshot:"
            "/opt/runtime-observer/snapshot:ro",
            self.source,
        )
        self.assertNotIn("runtime-failure-observer-http", self.source)
        self.assertNotIn("azdo-builds --definition", self.source)
        self.assertNotIn("helix-console --job-id", self.source)
        self.assertIn("entrypoint: /opt/runtime-observer/hlx/hlx", self.source)
        self.assertIn("entrypointArgs: [mcp]", self.source)
        self.assertIn("HOME: /home/hlx", self.source)

    def test_installs_exact_verified_hlx_package(self):
        self.assertIn("lewing.helix.mcp", self.source)
        self.assertIn("--version 0.11.0", self.source)
        self.assertIn(
            "https://packagefeedproxy.microsoft.io/nuget/v3/index.json",
            self.source,
        )
        self.assertIn(
            "757bdd7fd021a95b85ec6d1d7478138996fb5692ffb5172caaf457667e0b743e",
            self.source,
        )
        self.assertIn('test "$actual_sha" = "$HLX_PACKAGE_SHA256"', self.source)

    def test_github_tools_are_limited_to_issue_and_pr_reads(self):
        servers = {item["name"]: set(item["tools"]) for item in self.manifest["mcp_servers"]}
        self.assertEqual(servers["github"], {"search_issues", "issue_read", "search_pull_requests", "pull_request_read"})
        self.assertEqual(servers["hlx"], {"azdo_build", "azdo_timeline", "azdo_log", "azdo_search_log", "helix_logs", "helix_search"})
        self.assertEqual(servers["safeoutputs"], {"noop", "missing_tool", "missing_data", "create_pull_request"})

    def test_pull_requests_remain_drafts_with_a_two_output_cap(self):
        config = self.source.split("  create-pull-request:", 1)[1].split("\n---", 1)[0]
        for setting in ("draft: true", "max: 2", "protected-files: blocked"):
            self.assertIn(setting, config)
        allowed = set(re.findall(r'^\s{6}- "([^"]+)"$', config, re.MULTILINE))
        self.assertEqual(allowed, {
            "src/Microsoft.DotNet.XHarness.Apple/**",
            "src/Microsoft.DotNet.XHarness.Android/**",
            "src/Microsoft.DotNet.XHarness.CLI/**",
            "src/Microsoft.DotNet.XHarness.Common/**",
            "src/Microsoft.DotNet.XHarness.iOS.Shared/**",
            "src/Microsoft.DotNet.XHarness.TestRunners.*/**",
            "tests/Microsoft.DotNet.XHarness.Apple.Tests/**",
            "tests/Microsoft.DotNet.XHarness.Android.Tests/**",
            "tests/Microsoft.DotNet.XHarness.CLI.Tests/**",
            "tests/Microsoft.DotNet.XHarness.Common.Tests/**",
            "tests/Microsoft.DotNet.XHarness.iOS.Shared.Tests/**",
            "tests/Microsoft.DotNet.XHarness.TestRunners.Tests/**",
        })

    def test_main_agent_delegates_analysis_and_implementation(self):
        self.assertEqual(
            re.findall(r"^model: (\S+)$", self.source, re.MULTILINE),
            ["gpt-6.1-sol"] * 3,
        )
        self.assertIn("## agent: `runtime-candidate-analyst`", self.source)
        self.assertIn("## agent: `xharness-fix-implementer`", self.source)

    def test_diagnostics_are_short_lived_and_conditional(self):
        self.assertIn("name: runtime-observer-diagnostics-", self.source)
        self.assertIn("retention-days: 7", self.source)
        self.assertIn("/tmp/gh-aw/mcp-logs/", self.source)
        self.assertIn("/tmp/gh-aw/mcp-payloads/", self.source)
        self.assertIn("/tmp/gh-aw/sandbox/agent/logs/", self.source)
        self.assertIn("github.event.inputs.debug", self.source)
        self.assertIn("/tmp/gh-aw/agent/observer/source/", self.source)
        self.assertIn("/tmp/gh-aw/agent/observer/version-details/", self.source)
        self.assertIn("/tmp/gh-aw/agent/observer/hlx-manifests/", self.source)
        self.assertIn(
            "runtime-observer-unvalidated-raw-cache-", self.source
        )
        self.assertIn(
            "/tmp/gh-aw/agent/observer/xdg-cache/hlx/public/", self.source
        )
        self.assertIn("steps.raw-cache.outputs.upload == 'true'", self.source)
        self.assertIn("report-failed-jobs: false", self.source)

    def test_lock_is_compiled_with_expected_version_and_features(self):
        self.assertEqual(self.metadata["compiler_version"], "v0.89.17")
        self.assertEqual(self.metadata["agent_model"], "gpt-6.1-sol")
        self.assertIn("runtime-failure-observer-collect", self.lock)
        self.assertIn("python3 .github/workflows/runtime-failure-observer-handoff.py", self.lock)
        self.assertIn("HLX_EVAL_SNAPSHOT", self.lock)
        images = {item["image"] for item in self.manifest["containers"]}
        self.assertIn(
            "mcr.microsoft.com/dotnet/runtime:10.0@sha256:"
            "b89586dc17781f25531909993658aa8161205ae38b8cec8847df4a8221a403d5",
            images,
        )


if __name__ == "__main__":
    unittest.main()
