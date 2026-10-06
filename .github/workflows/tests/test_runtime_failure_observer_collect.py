import datetime as dt
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "runtime-failure-observer-collect"
LOADER = importlib.machinery.SourceFileLoader("observer_collect", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
observer_collect = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = observer_collect
LOADER.exec_module(observer_collect)

REPO = Path(__file__).parents[3]
TEST_TEMP_ROOT = REPO / "artifacts" / "observer-validation" / "collector-tests"
NOW = dt.datetime(2026, 10, 5, 12, 3, 42, tzinfo=dt.timezone.utc)
XHARNESS_SHA = "4f2266d4c147bd8af3fe4dff129f725ddb9dce57"
OTHER_XHARNESS_SHA = "64629bf5e3794db48c3fc08fe30e16c5f818096f"
RUNTIME_SHA = "d1920aa3fa8523a7b8d04d6e442faccb0b0bb68e"
JOB_1 = "00000000-0000-0000-0000-000000000001"
JOB_2 = "00000000-0000-0000-0000-000000000002"


def evidence(value, kind, **identity):
    return observer_collect.EvidenceValue(
        value,
        {"kind": kind, **identity},
    )


def text_evidence(text, kind, outcome="ok", detail=None, **identity):
    return observer_collect.TextEvidence(
        text,
        {"kind": kind, **identity},
        outcome,
        detail,
    )


def build(build_id, finish, definition_id=154, runtime_sha=RUNTIME_SHA):
    return {
        "id": build_id,
        "definition": {"id": definition_id},
        "result": "failed",
        "finishTime": finish,
        "sourceVersion": runtime_sha,
        "_links": {
            "web": {
                "href": (
                    "https://dev.azure.com/dnceng-public/public/_build/results"
                    f"?buildId={build_id}"
                )
            }
        },
    }


def submission(log_id, name="Send tests to Helix (Unix)", result="succeeded"):
    return {
        "type": "Task",
        "name": name,
        "result": result,
        "log": {"id": log_id},
    }


def submission_log(*jobs):
    return "\n".join(f"Sent Helix Job: {job}" for job in jobs)


def console(exit_code=70, *, signature="error: Run timed out after 120 seconds"):
    return "\n".join(
        [
            "Microsoft.DotNet.XHarness.CLI 11.0.0+" + XHARNESS_SHA,
            "Running command: /agent/_work/1/s/xharness apple test --app /payload/app.app",
            signature,
            f"Exit code: {exit_code}",
        ]
    )


class FakeProvider:
    version = "fixture-hlx"
    metadata = {
        "commit": "fixture-commit",
        "package": "fixture-package",
        "packageSha256": "fixture-package-sha256",
    }

    def __init__(self):
        self.build_lists = {}
        self.timelines = {}
        self.logs = {}
        self.work_items = {}
        self.consoles = {}
        self.console_requests = []
        self.finalized = []
        self.snapshot_complete = None

    def list_builds(self, definition_id, limit):
        value = self.build_lists.get(definition_id, [])
        if isinstance(value, observer_collect.EvidenceValue):
            return value
        return evidence(
            {"count": len(value), "value": value},
            "azdo-build-list",
            definitionId=definition_id,
            limit=limit,
        )

    def prepare(self, selected_builds):
        return None

    def timeline(self, build_id):
        value = self.timelines.get(build_id, {"records": []})
        if isinstance(value, observer_collect.EvidenceValue):
            return value
        return evidence(value, "azdo-timeline", buildId=build_id)

    def build_log(self, build_id, log_id):
        return self.logs[(build_id, log_id)]

    def helix_work_items(self, job_id):
        value = self.work_items[job_id]
        if isinstance(value, observer_collect.EvidenceValue):
            return value
        return evidence(value, "helix-work-items", jobId=job_id)

    def helix_console(self, job_id, work_item_name):
        self.console_requests.append((job_id, work_item_name))
        return self.consoles[(job_id, work_item_name)]

    def finalize(self, *, complete):
        self.finalized.append(complete)
        return {
            "layout": "fixture",
            "complete": (
                complete if self.snapshot_complete is None else self.snapshot_complete
            ),
            "path": "hlx-snapshot",
        }


class FakeResolver:
    def __init__(self, sha=XHARNESS_SHA, error=None):
        self.sha = sha
        self.error = error
        self.requests = []

    def resolve(self, runtime_sha):
        self.requests.append(runtime_sha)
        if self.error:
            raise self.error
        content = (
            "<Dependencies><ToolsetDependencies>"
            '<Dependency Name="Microsoft.DotNet.XHarness.CLI">'
            "<Uri>https://github.com/dotnet/xharness</Uri>"
            f"<Sha>{self.sha}</Sha>"
            "</Dependency></ToolsetDependencies></Dependencies>"
        ).encode()
        return {
            "xharnessSha": self.sha,
            "provenance": {
                "kind": "runtime-version-details",
                "runtimeSha": runtime_sha,
            },
            "content": content,
        }


class FakeSourcePreparer:
    def __init__(self, error=None):
        self.error = error
        self.requested = None

    def starting_revision(self):
        return XHARNESS_SHA

    def prepare(self, consumed_shas, output_dir):
        self.requested = set(consumed_shas)
        if self.error:
            raise self.error
        return {
            "startingRevision": XHARNESS_SHA,
            "endingRevision": XHARNESS_SHA,
            "checkoutUnchanged": True,
            "requiredConsumedRevisions": [
                {"sha": sha, "fetched": False} for sha in sorted(consumed_shas)
            ],
            "package": {"kind": "fixture", "path": "source/fixture"},
        }


class CollectorTestCase(unittest.TestCase):
    def setUp(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.output_dir = Path(self.temp.name) / "output"
        self.provider = FakeProvider()
        self.resolver = FakeResolver()
        self.source = FakeSourcePreparer()

    def tearDown(self):
        self.temp.cleanup()

    def run_collector(self):
        collector = observer_collect.ObserverCollector(
            provider=self.provider,
            version_resolver=self.resolver,
            source_preparer=self.source,
            output_dir=self.output_dir,
            now=NOW,
        )
        result = collector.run()
        manifest = json.loads(
            (self.output_dir / "observer-manifest.json").read_text()
        )
        index = json.loads((self.output_dir / "failure-index.json").read_text())
        return result, manifest, index

    def add_build(
        self,
        build_id=100,
        *,
        finish="2026-10-05T10:00:00Z",
        timeline=None,
        definition_id=154,
        runtime_sha=RUNTIME_SHA,
    ):
        self.provider.build_lists.setdefault(definition_id, []).append(
            build(build_id, finish, definition_id, runtime_sha)
        )
        self.provider.timelines[build_id] = timeline or {"records": []}

    def add_job(
        self,
        build_id,
        log_id,
        job_id,
        *,
        work_items,
        log_text=None,
    ):
        self.provider.logs[(build_id, log_id)] = text_evidence(
            log_text if log_text is not None else submission_log(job_id),
            "azdo-log",
            buildId=build_id,
            logId=log_id,
        )
        self.provider.work_items[job_id] = work_items


class PolicyAndTraversalTests(CollectorTestCase):
    def test_complete_empty_is_explicit(self):
        result, manifest, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertTrue(manifest["complete"])
        self.assertTrue(manifest["empty"])
        self.assertEqual(manifest["counts"]["sourceImprovementCandidates"], 0)
        self.assertEqual(manifest["provider"]["commit"], "fixture-commit")
        self.assertEqual(manifest["provider"]["package"], "fixture-package")
        self.assertEqual(index["candidates"], [])
        self.assertEqual(self.source.requested, set())
        self.assertEqual(self.provider.finalized, [True])

    def test_traverses_successful_submissions_without_monitor_entries(self):
        self.add_build(
            timeline={
                "records": [
                    submission(10),
                    submission(11, name="Send to Helix"),
                ]
            }
        )
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[{"Name": "ios", "ExitCode": 1}],
        )
        self.add_job(
            100,
            11,
            JOB_2,
            work_items=[{"WorkItemName": "android", "ExitCode": 2}],
        )
        self.provider.consoles[(JOB_1, "ios")] = text_evidence(
            console(70), "helix-console", jobId=JOB_1, workItemName="ios"
        )
        self.provider.consoles[(JOB_2, "android")] = text_evidence(
            console(83, signature="error: app failed to launch"),
            "helix-console",
            jobId=JOB_2,
            workItemName="android",
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertEqual(manifest["counts"]["helixJobs"], 2)
        self.assertEqual(len(index["candidates"]), 2)
        self.assertEqual(
            manifest["traversal"],
            [
                {
                    "definitionId": 154,
                    "buildId": 100,
                    "buildRole": "source",
                    "jobId": JOB_1,
                    "inventory": {
                        "kind": "helix-work-items",
                        "jobId": JOB_1,
                    },
                    "workItems": [{"name": "ios", "exitCode": 1}],
                },
                {
                    "definitionId": 154,
                    "buildId": 100,
                    "buildRole": "source",
                    "jobId": JOB_2,
                    "inventory": {
                        "kind": "helix-work-items",
                        "jobId": JOB_2,
                    },
                    "workItems": [{"name": "android", "exitCode": 2}],
                },
            ],
        )
        self.assertEqual(
            {candidate["xharnessExitCode"] for candidate in index["candidates"]},
            {70, 83},
        )

    def test_negative_helix_exit_is_skipped_before_console_fetch(self):
        self.add_build(timeline={"records": [submission(10)]})
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[
                {"Name": "dead-letter", "ExitCode": -100},
                {"Name": "device-test", "ExitCode": 17},
            ],
        )
        self.provider.consoles[(JOB_1, "device-test")] = text_evidence(
            console(81, signature="error: device was not found"),
            "helix-console",
            jobId=JOB_1,
            workItemName="device-test",
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertEqual(self.provider.console_requests, [(JOB_1, "device-test")])
        candidate = index["candidates"][0]
        self.assertEqual(candidate["helixExitCode"], 17)
        self.assertEqual(candidate["xharnessExitCode"], 81)
        self.assertIn(
            "helix_infrastructure_exit_code",
            {skip["reason"] for skip in manifest["skips"]},
        )

    def test_failed_submission_is_not_read(self):
        self.add_build(
            timeline={
                "records": [
                    submission(10, result="failed"),
                    submission(11, result="skipped"),
                ]
            }
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 0)
        self.assertTrue(manifest["empty"])
        self.assertEqual(
            [skip["reason"] for skip in manifest["skips"]],
            ["helix_job_not_submitted", "helix_job_not_submitted"],
        )

    def test_empty_and_canonical_404_are_retention_skips(self):
        self.add_build(timeline={"records": [submission(10)]})
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[
                {"Name": "empty", "ExitCode": 1},
                {"Name": "missing", "ExitCode": 2},
                {"Name": "rolled-off", "ExitCode": 3},
            ],
        )
        self.provider.consoles[(JOB_1, "empty")] = text_evidence(
            "",
            "helix-console",
            jobId=JOB_1,
            workItemName="empty",
        )
        self.provider.consoles[(JOB_1, "missing")] = text_evidence(
            None,
            "helix-console",
            outcome="console_404",
            detail="canonical console endpoint returned 404",
            jobId=JOB_1,
            workItemName="missing",
        )
        self.provider.consoles[(JOB_1, "rolled-off")] = text_evidence(
            None,
            "helix-console",
            outcome="work_item_unavailable",
            detail="work-item inventory no longer contains the selected name",
            jobId=JOB_1,
            workItemName="rolled-off",
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 0)
        self.assertTrue(manifest["empty"])
        self.assertEqual(
            {skip["reason"] for skip in manifest["skips"]},
            {
                "empty_helix_evidence",
                "helix_console_unavailable_404",
                "helix_work_item_unavailable",
            },
        )

    def test_missing_job_identifier_is_retention_skip(self):
        self.add_build(timeline={"records": [submission(10)]})
        self.provider.logs[(100, 10)] = text_evidence(
            "submission completed, but retained completion line is absent",
            "azdo-log",
            buildId=100,
            logId=10,
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 0)
        self.assertTrue(manifest["empty"])
        self.assertIn(
            "helix_job_identifier_unavailable",
            {skip["reason"] for skip in manifest["skips"]},
        )

    def test_required_console_failure_makes_run_incomplete_not_empty(self):
        self.add_build(timeline={"records": [submission(10)]})
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[{"Name": "ios", "ExitCode": 1}],
        )
        self.provider.consoles[(JOB_1, "ios")] = text_evidence(
            None,
            "helix-console",
            outcome="transport_error",
            detail="connection reset",
            jobId=JOB_1,
            workItemName="ios",
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 2)
        self.assertFalse(manifest["complete"])
        self.assertFalse(manifest["empty"])
        self.assertEqual(manifest["errors"][0]["code"], "helix_console_unavailable")
        self.assertEqual(self.provider.finalized, [False])

    def test_missing_console_url_matches_original_incomplete_policy(self):
        self.add_build(
            1622443,
            timeline={"records": [submission(10)]},
        )
        self.add_job(
            1622443,
            10,
            JOB_1,
            work_items=[
                {
                    "Name": "System.IO.FileSystem.DisabledFileLocking.Tests",
                    "ExitCode": 999,
                }
            ],
        )
        self.provider.consoles[
            (JOB_1, "System.IO.FileSystem.DisabledFileLocking.Tests")
        ] = text_evidence(
            None,
            "helix-console",
            outcome="missing_console_url",
            detail="Helix work item did not contain a console output URL",
            jobId=JOB_1,
            workItemName="System.IO.FileSystem.DisabledFileLocking.Tests",
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 2)
        self.assertFalse(manifest["complete"])
        self.assertFalse(manifest["empty"])
        self.assertEqual(index["candidates"], [])
        self.assertEqual(manifest["errors"][0]["code"], "helix_console_url_missing")
        self.assertEqual(
            manifest["errors"][0]["message"],
            "Helix work item did not contain a console output URL",
        )
        self.assertEqual(
            manifest["errors"][0]["scope"]["helixExitCode"],
            999,
        )
        self.assertNotIn(
            "helix_console_url_missing",
            {skip["reason"] for skip in manifest["skips"]},
        )

    def test_incomplete_snapshot_cannot_be_successful_empty(self):
        self.provider.snapshot_complete = False

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 2)
        self.assertFalse(manifest["complete"])
        self.assertFalse(manifest["empty"])
        self.assertEqual(manifest["errors"][0]["code"], "incomplete_snapshot")


class InvocationAndNormalizationTests(CollectorTestCase):
    def test_multiple_xharness_invocations_are_indexed_separately(self):
        self.add_build(timeline={"records": [submission(10)]})
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[{"Name": "combined", "ExitCode": 1}],
        )
        combined = "\n".join(
            [
                "Running command: /work/xharness apple test --app /payload/a.app",
                "error: first run timed out",
                "Exit code: 70",
                "Running command: /work/xharness android test --app /payload/b.apk",
                "error: second app failed to launch",
                "Process exited with code 83",
            ]
        )
        self.provider.consoles[(JOB_1, "combined")] = text_evidence(
            combined,
            "helix-console",
            jobId=JOB_1,
            workItemName="combined",
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertEqual(manifest["counts"]["xharnessInvocations"], 2)
        self.assertEqual(
            [candidate["xharnessExitCode"] for candidate in index["candidates"]],
            [70, 83],
        )

    def test_normalization_removes_volatile_values(self):
        first = (
            "Error on helix1234: /agent/a/device "
            "00000000-0000-0000-0000-000000000001 timed out after 120 seconds "
            "at 2026-10-05T10:00:00Z"
        )
        second = (
            "Error on helix9876: /agent/b/device "
            "00000000-0000-0000-0000-000000000099 timed out after 90 seconds "
            "at 2026-10-05T11:00:00Z"
        )
        self.assertEqual(
            observer_collect.normalize_signature(first),
            observer_collect.normalize_signature(second),
        )

    def test_command_without_exit_code_is_required_missing_data(self):
        self.add_build(timeline={"records": [submission(10)]})
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[
                {"Name": "ios", "ExitCode": 1},
                {"Name": "android", "ExitCode": 2},
            ],
        )
        self.provider.consoles[(JOB_1, "ios")] = text_evidence(
            "Running command: /work/xharness apple test\nerror: timed out",
            "helix-console",
            jobId=JOB_1,
            workItemName="ios",
        )
        self.provider.consoles[(JOB_1, "android")] = text_evidence(
            console(81, signature="error: device was not found"),
            "helix-console",
            jobId=JOB_1,
            workItemName="android",
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 2)
        self.assertEqual(
            self.provider.console_requests,
            [(JOB_1, "ios"), (JOB_1, "android")],
        )
        self.assertEqual(len(index["candidates"]), 1)
        error = manifest["errors"][0]
        self.assertEqual(error["code"], "missing_xharness_exit_code")
        self.assertEqual(error["scope"]["buildId"], 100)
        self.assertEqual(error["scope"]["jobId"], JOB_1)
        self.assertEqual(error["scope"]["workItemName"], "ios")


class HistoryAndSourceTests(CollectorTestCase):
    def _add_console_build(self, build_id, finish, *, informational=True):
        self.add_build(
            build_id,
            finish=finish,
            timeline={"records": [submission(build_id)]},
        )
        job_id = f"00000000-0000-0000-0000-{build_id:012d}"
        self.add_job(
            build_id,
            build_id,
            job_id,
            work_items=[{"Name": f"work-{build_id}", "ExitCode": 1}],
        )
        body = console(70)
        if not informational:
            body = "\n".join(body.splitlines()[1:])
        self.provider.consoles[(job_id, f"work-{build_id}")] = text_evidence(
            body,
            "helix-console",
            jobId=job_id,
            workItemName=f"work-{build_id}",
        )

    def test_selects_source_and_at_most_five_preceding_failed_builds(self):
        for offset in range(7):
            finish = NOW - dt.timedelta(hours=offset + 1)
            self._add_console_build(
                200 - offset,
                finish.isoformat().replace("+00:00", "Z"),
            )
        job_195 = "00000000-0000-0000-0000-000000000195"
        self.provider.consoles[(job_195, "work-195")] = text_evidence(
            console(70, signature="error: a different failure signature"),
            "helix-console",
            jobId=job_195,
            workItemName="work-195",
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertEqual(len(manifest["builds"]), 6)
        self.assertEqual(manifest["definitions"][0]["sourceBuildId"], 200)
        self.assertEqual(
            manifest["definitions"][0]["historyBuildIds"],
            [199, 198, 197, 196, 195],
        )
        source = next(
            candidate for candidate in index["candidates"] if candidate["buildRole"] == "source"
        )
        self.assertEqual(source["recurrence"]["matchingBuildCount"], 5)
        self.assertTrue(source["recurrence"]["strong"])

    def test_analysis_projection_is_bounded_and_preserves_recurrence(self):
        for build_id, finish in (
            (100, "2026-10-05T10:00:00Z"),
            (99, "2026-10-04T10:00:00Z"),
            (98, "2026-10-03T10:00:00Z"),
        ):
            self._add_console_build(build_id, finish)
        result, manifest, index = self.run_collector()
        self.assertEqual(result, 0)
        history = next(item for item in index["candidates"] if item["buildRole"] == "history")
        index["candidates"].extend([history] * 1000)
        manifest["traversal"] = ["unused large inventory"] * 10000
        manifest["skips"] = [
            {"kind": "policy", "reason": "no_xharness_invocation"}
        ] * 10000
        inputs = observer_collect.build_analysis_inputs(manifest, index)
        self.assertEqual(len(inputs["candidates"]), 1)
        self.assertEqual([w["buildId"] for w in inputs["candidates"][0]["history"]], [99, 98])
        self.assertEqual(inputs["acquisitionSkipCounts"], [
            {"kind": "policy", "reason": "no_xharness_invocation", "count": 10000}
        ])
        self.assertNotIn("traversal", inputs["manifest"])
        self.assertNotIn("skips", inputs["manifest"])
        self.assertLess(len(json.dumps(inputs)), 8192)
        persisted = json.loads((self.output_dir / "analysis-inputs.json").read_text())
        self.assertEqual(persisted["manifest"]["complete"], manifest["complete"])
        self.assertEqual((self.output_dir / "filed.tsv").read_text(), "")

    def test_incomplete_projection_does_not_require_derived_candidate_fields(self):
        manifest = {
            "schemaVersion": 1, "kind": "xharness.runtime-failure-observer.collect",
            "status": "incomplete", "complete": False, "empty": False,
            "asOf": "2026-10-05T12:03:42Z", "policy": {}, "counts": {},
            "coverage": {}, "source": {}, "versionFiles": [], "skips": [],
            "errors": [{"code": "missing_console"}],
        }
        inputs = observer_collect.build_analysis_inputs(manifest, {"candidates": [{}]})
        self.assertFalse(inputs["manifest"]["complete"])
        self.assertEqual(inputs["manifest"]["errors"], manifest["errors"])
        self.assertEqual(inputs["candidates"], [])

    def test_informational_version_avoids_runtime_version_fetch(self):
        self._add_console_build(100, "2026-10-05T10:00:00Z")

        result, _, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertEqual(self.resolver.requests, [])
        self.assertEqual(self.source.requested, {XHARNESS_SHA})
        self.assertEqual(
            index["candidates"][0]["sourceResolution"]["kind"],
            "xharness-informational-version",
        )

    def test_source_and_one_history_build_do_not_satisfy_recurrence(self):
        self._add_console_build(100, "2026-10-05T10:00:00Z")
        self._add_console_build(99, "2026-10-04T10:00:00Z")
        result, _, index = self.run_collector()
        self.assertEqual(result, 0)
        source = next(item for item in index["candidates"] if item["buildRole"] == "source")
        self.assertEqual(source["recurrence"]["matchingBuildCount"], 2)
        self.assertEqual(source["recurrence"]["matchingHistoryBuildIds"], [99])
        self.assertEqual(source["recurrence"]["matchingHistoryBuildCount"], 1)
        self.assertFalse(source["recurrence"]["strong"])
        self.assertFalse(source["analysisRequired"])
        self.assertEqual(source["analysisSkipReason"], "weak signature")

    def test_duplicate_source_tuple_is_analyzed_once(self):
        for build_id, finish in (
            (100, "2026-10-05T10:00:00Z"),
            (99, "2026-10-04T10:00:00Z"),
            (98, "2026-10-03T10:00:00Z"),
        ):
            self._add_console_build(build_id, finish)
        job = "00000000-0000-0000-0000-000000000100"
        self.provider.work_items[job].append({"Name": "duplicate-work-item", "ExitCode": 1})
        self.provider.consoles[(job, "duplicate-work-item")] = self.provider.consoles[(job, "work-100")]
        result, _, index = self.run_collector()
        self.assertEqual(result, 0)
        source = [item for item in index["candidates"] if item["buildRole"] == "source"]
        self.assertEqual(len(source), 2)
        self.assertEqual([item["analysisRequired"] for item in source], [True, False])
        self.assertEqual(source[1]["analysisSkipReason"], "duplicate source signature")

    def test_runtime_version_details_fallback_is_persisted(self):
        self._add_console_build(
            100,
            "2026-10-05T10:00:00Z",
            informational=False,
        )

        result, manifest, index = self.run_collector()

        self.assertEqual(result, 0)
        self.assertEqual(self.resolver.requests, [RUNTIME_SHA])
        self.assertEqual(self.source.requested, {XHARNESS_SHA})
        self.assertEqual(
            index["candidates"][0]["sourceResolution"]["kind"],
            "runtime-version-details",
        )
        self.assertEqual(len(manifest["versionFiles"]), 1)
        version_file = self.output_dir / manifest["versionFiles"][0]["path"]
        self.assertTrue(version_file.is_file())

    def test_missing_runtime_revision_makes_source_preparation_incomplete(self):
        self.add_build(
            100,
            timeline={"records": [submission(10)]},
            runtime_sha=None,
        )
        self.add_job(
            100,
            10,
            JOB_1,
            work_items=[{"Name": "ios", "ExitCode": 1}],
        )
        body = "\n".join(console(70).splitlines()[1:])
        self.provider.consoles[(JOB_1, "ios")] = text_evidence(
            body,
            "helix-console",
            jobId=JOB_1,
            workItemName="ios",
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 2)
        self.assertEqual(manifest["errors"][0]["code"], "missing_runtime_revision")
        self.assertFalse(manifest["empty"])

    def test_version_resolution_error_is_reported(self):
        self._add_console_build(
            100,
            "2026-10-05T10:00:00Z",
            informational=False,
        )
        self.resolver.error = observer_collect.CollectorError(
            "version_details_unavailable", "not retained"
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 2)
        self.assertEqual(
            manifest["errors"][0]["code"], "version_details_unavailable"
        )

    def test_source_preparation_error_is_reported(self):
        self._add_console_build(100, "2026-10-05T10:00:00Z")
        self.source.error = observer_collect.CollectorError(
            "xharness_revision_unavailable", "missing commit"
        )

        result, manifest, _ = self.run_collector()

        self.assertEqual(result, 2)
        self.assertEqual(
            manifest["errors"][0]["code"], "xharness_revision_unavailable"
        )


class ParserTests(unittest.TestCase):
    def test_signature_uses_cli_failure_instead_of_structured_artifact_metadata(self):
        body = "\n".join([
            "XHarness command issued: apple test --app tests.app",
            "[03:18:21] fail: Run timed out after 10800 seconds",
            *("[03:18:27] dbug: streamed system log" for _ in range(60)),
            "<<XHARNESS_RESULT_START>>",
            json.dumps({"files": [{"type": "crash-reports-before-launch"}]}, indent=2),
            "<<XHARNESS_RESULT_END>>",
            "XHarness exit code: 70 (TIMED_OUT)",
        ])
        candidate = observer_collect.parse_xharness_invocations(body)[0]
        self.assertEqual(candidate["signatureNorm"], "fail: run timed out after <duration>")
        self.assertNotIn("crash-reports", candidate["signature"])
        other = observer_collect.parse_xharness_invocations(
            body.replace("03:18:21", "07:40:03").replace("10800 seconds", "600 seconds")
        )[0]
        self.assertEqual(candidate["signatureNorm"], other["signatureNorm"])

    def test_alias_expansion_and_child_exit_codes_form_one_invocation(self):
        invocations = observer_collect.parse_xharness_invocations(
            "\n".join(
                [
                    "++ xharness android test --app tests.apk --timeout 00:30:00",
                    "++ dotnet exec /work/Microsoft.DotNet.XHarness.CLI.dll android test --app tests.apk --timeout 00:30:00",
                    "+ sleep 2280",
                    "[11.0.0-prerelease+" + XHARNESS_SHA + "] XHarness command issued: android test --app tests.apk --timeout 00:30:00",
                    "[05:18:36] dbug: Failed to read optional device property",
                    "                 Exit code: 1",
                    "[05:19:50] dbug: Exit code: 0",
                    "error: tests failed",
                    "XHarness exit code: 1 (TESTS_FAILED)",
                ]
            )
        )

        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0]["exitCode"], 1)
        self.assertEqual(invocations[0]["lineRange"]["invocation"], 4)
        self.assertEqual(invocations[0]["informationalVersionSha"], XHARNESS_SHA)
        self.assertEqual(
            invocations[0]["command"],
            "android test --app tests.apk --timeout 00:30:00",
        )

    def test_tests_failed_exit_is_not_an_improvement_candidate(self):
        collector = object.__new__(observer_collect.ObserverCollector)
        collector.candidates = []
        invocation = observer_collect.parse_xharness_invocations(
            "\n".join(
                [
                    "++ xharness android test --app tests.apk",
                    "++ dotnet exec /work/Microsoft.DotNet.XHarness.CLI.dll android test --app tests.apk",
                    "[11.0.0-prerelease+" + XHARNESS_SHA + "] XHarness command issued: android test --app tests.apk",
                    "[00:00:01] dbug: Exit code: 0",
                    "error: tests failed",
                    "XHarness exit code: 1 (TESTS_FAILED)",
                ]
            )
        )[0]
        collector._record_candidate(
            build={
                "definitionId": 154,
                "definitionName": "runtime-extra-platforms",
                "id": 100,
                "role": "source",
                "url": "https://example.invalid/build/100",
                "runtimeSha": RUNTIME_SHA,
            },
            job_id=JOB_1,
            work_item={"name": "tests", "exitCode": 1},
            console=text_evidence(
                "fixture",
                "hlx-console",
                jobId=JOB_1,
                workItemName="tests",
            ),
            invocation=invocation,
        )

        self.assertEqual(len(collector.candidates), 1)
        self.assertEqual(collector.candidates[0]["xharnessExitCode"], 1)
        self.assertFalse(collector.candidates[0]["improvementTarget"])

    def test_explicit_markers_preserve_multiple_invocations(self):
        invocations = observer_collect.parse_xharness_invocations(
            "\n".join(
                [
                    "[11.0.0-prerelease] XHarness command issued: android test --app first.apk",
                    "[00:00:01] dbug: Exit code: 0",
                    "error: first test failed",
                    "XHarness exit code: 1 (TESTS_FAILED)",
                    "[11.0.0-prerelease+" + OTHER_XHARNESS_SHA + "] XHarness command issued: android test --app second.apk",
                    "error: device not found",
                    "XHarness exit code: 81 (DEVICE_NOT_FOUND)",
                ]
            )
        )

        self.assertEqual(
            [(item["command"], item["exitCode"]) for item in invocations],
            [
                ("android test --app first.apk", 1),
                ("android test --app second.apk", 81),
            ],
        )
        self.assertIsNone(invocations[0]["informationalVersionSha"])
        self.assertEqual(
            invocations[1]["informationalVersionSha"],
            OTHER_XHARNESS_SHA,
        )

    def test_completed_trace_is_not_coalesced_with_repeated_explicit_command(self):
        invocations = observer_collect.parse_xharness_invocations(
            "\n".join(
                [
                    "Running command: xharness android test --app tests.apk",
                    "error: first run timed out",
                    "Exit code: 70",
                    "[11.0.0-prerelease+" + XHARNESS_SHA + "] XHarness command issued: android test --app tests.apk",
                    "error: device not found",
                    "XHarness exit code: 81 (DEVICE_NOT_FOUND)",
                ]
            )
        )

        self.assertEqual(
            [item["exitCode"] for item in invocations],
            [70, 81],
        )

    def test_explicit_command_without_xharness_terminal_marker_is_missing(self):
        with self.assertRaises(observer_collect.CollectorError) as raised:
            observer_collect.parse_xharness_invocations(
                "\n".join(
                    [
                        "[11.0.0-prerelease+" + XHARNESS_SHA + "] XHarness command issued: android test --app tests.apk",
                        "[00:00:01] dbug: Exit code: 1",
                        "Helix work item exited with code 1",
                    ]
                )
            )

        self.assertEqual(raised.exception.code, "missing_xharness_exit_code")

    def test_dotnet_exec_xharness_command_is_detected(self):
        invocations = observer_collect.parse_xharness_invocations(
            "\n".join(
                [
                    "Running command: dotnet exec /work/Microsoft.DotNet.XHarness.CLI.dll android test",
                    "error: package install failed",
                    "ExitCode=78",
                ]
            )
        )

        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0]["exitCode"], 78)

    def test_xharness_version_label_provides_consumed_revision(self):
        invocations = observer_collect.parse_xharness_invocations(
            "\n".join(
                [
                    "XHarness version: 11.0.0-prerelease+" + XHARNESS_SHA,
                    "Running command: xharness wasm test",
                    "error: browser timed out",
                    "Exit code: 70",
                ]
            )
        )

        self.assertEqual(
            invocations[0]["informationalVersionSha"],
            XHARNESS_SHA,
        )

    def test_version_details_accepts_repeated_packages_at_one_revision(self):
        document = (
            "<Dependencies><ToolsetDependencies>"
            '<Dependency Name="Microsoft.DotNet.XHarness.CLI">'
            "<Uri>https://github.com/dotnet/xharness</Uri>"
            f"<Sha>{XHARNESS_SHA}</Sha></Dependency>"
            '<Dependency Name="Microsoft.DotNet.XHarness.TestRunners.Common">'
            "<Uri>https://github.com/dotnet/xharness/</Uri>"
            f"<Sha>{XHARNESS_SHA}</Sha></Dependency>"
            "</ToolsetDependencies></Dependencies>"
        ).encode()

        self.assertEqual(
            observer_collect.parse_version_details(document),
            XHARNESS_SHA,
        )

    def test_version_details_rejects_multiple_xharness_revisions(self):
        document = (
            "<Dependencies><ToolsetDependencies>"
            "<Dependency><Uri>https://github.com/dotnet/xharness</Uri>"
            f"<Sha>{XHARNESS_SHA}</Sha></Dependency>"
            "<Dependency><Uri>https://github.com/dotnet/xharness</Uri>"
            f"<Sha>{OTHER_XHARNESS_SHA}</Sha></Dependency>"
            "</ToolsetDependencies></Dependencies>"
        ).encode()

        with self.assertRaisesRegex(
            observer_collect.CollectorError,
            "exactly one XHarness revision",
        ):
            observer_collect.parse_version_details(document)

    def test_incomplete_build_list_is_not_successful_empty(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            provider = FakeProvider()
            provider.build_lists[154] = observer_collect.EvidenceValue(
                {"value": []},
                {"kind": "azdo-build-list", "definitionId": 154},
                complete=False,
            )
            collector = observer_collect.ObserverCollector(
                provider=provider,
                version_resolver=FakeResolver(),
                source_preparer=FakeSourcePreparer(),
                output_dir=Path(temporary) / "output",
                now=NOW,
            )

            self.assertEqual(collector.run(), 2)
            manifest = json.loads(
                (Path(temporary) / "output" / "observer-manifest.json").read_text()
            )
            self.assertFalse(manifest["complete"])
            self.assertFalse(manifest["empty"])
            self.assertEqual(manifest["errors"][0]["code"], "incomplete_build_list")


class GitSourcePreparerTests(unittest.TestCase):
    def test_packages_required_source_without_moving_checkout(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        starting_revision = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            preparer = observer_collect.GitSourcePreparer(REPO)

            manifest = preparer.prepare(
                {starting_revision},
                Path(temporary),
            )

            package = Path(temporary) / manifest["package"]["path"]
            packaged_start = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(package),
                    "rev-parse",
                    "refs/heads/observer-start",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            packaged_consumed = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(package),
                    "rev-parse",
                    f"refs/heads/consumed/{starting_revision}",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            remotes = subprocess.run(
                ["git", "--git-dir", str(package), "remote"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        ending_revision = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertEqual(packaged_start, starting_revision)
        self.assertEqual(packaged_consumed, starting_revision)
        self.assertEqual(remotes, "")
        self.assertEqual(ending_revision, starting_revision)
        self.assertTrue(manifest["checkoutUnchanged"])


class HlxProviderTests(unittest.TestCase):
    def test_command_timeout_is_structured(self):
        with patch.object(observer_collect, "HLX_COMMAND_TIMEOUT", 0.05):
            with self.assertRaises(observer_collect.CollectorError) as raised:
                observer_collect.HlxV011Provider._execute(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    check=False,
                )
        self.assertEqual(raised.exception.code, "hlx_command_timeout")

    def test_mcp_deadline_and_stderr_drain(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = Path(temporary) / "hlx"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, sys, time\n"
                "if sys.argv[1] == '--version':\n"
                "    print('0.11.0'); raise SystemExit(0)\n"
                "for line in sys.stdin:\n"
                "    request = json.loads(line)\n"
                "    if request['method'] == 'initialize':\n"
                "        sys.stderr.write('diagnostic' * 100000); sys.stderr.flush()\n"
                "        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n"
                "    elif request['method'] == 'tools/call':\n"
                "        time.sleep(5)\n"
            )
            executable.chmod(0o755)
            provider = observer_collect.HlxV011Provider(executable, Path(temporary) / "output")
            with patch.object(observer_collect, "HLX_MCP_TIMEOUT", 0.1):
                with self.assertRaises(observer_collect.CollectorError) as raised:
                    provider._mcp_call("helix_logs", {})
        self.assertEqual(raised.exception.code, "hlx_mcp_timeout")
        self.assertEqual(raised.exception.scope["method"], "tools/call")

    def _fake_hlx(self, directory, version, builds=None, wrap_builds=False):
        executable = Path(directory) / "hlx"
        build_payload = (
            {"count": len(builds or []), "value": builds or []}
            if wrap_builds
            else builds or []
        )
        build_json = json.dumps(build_payload)
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            f"  printf '%s\\n' '{version}'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "azdo" ] && [ "$2" = "builds" ]; then\n'
            f"  printf '%s\\n' '{build_json}'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "cache" ] && [ "$2" = "status" ]; then\n'
            "  printf 'Cache status (public (unauthenticated)):\\n'\n"
            "  printf '  Cache dir:       %s/hlx/public\\n' \"$XDG_CACHE_HOME\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 9\n"
        )
        executable.chmod(0o755)
        return executable

    def _fake_collecting_hlx(self, directory, *, validation_exit=0):
        executable = Path(directory) / "hlx"
        calls = Path(directory) / "calls.jsonl"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            f"calls = pathlib.Path({str(calls)!r})\n"
            "args = sys.argv[1:]\n"
            "with calls.open('a') as stream:\n"
            "    stream.write(json.dumps(args) + '\\n')\n"
            "if args == ['--version']:\n"
            f"    print({observer_collect.HLX_VERSION!r})\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['collect', 'azdo-build']:\n"
            "    manifest = pathlib.Path(args[args.index('--manifest') + 1])\n"
            "    cache = pathlib.Path(args[args.index('--cache-dir') + 1])\n"
            "    cache.mkdir(parents=True, exist_ok=True)\n"
            "    manifest.parent.mkdir(parents=True, exist_ok=True)\n"
            "    manifest.write_text(json.dumps({'complete': True, 'incompleteDetails': []}))\n"
            "    if '--export' in args:\n"
            "        snapshot = pathlib.Path(args[args.index('--export') + 1])\n"
            "        snapshot.mkdir(parents=True)\n"
            "        (snapshot / 'cache.db').write_bytes(b'fixture')\n"
            "    print(json.dumps({'complete': True}))\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['snapshot', 'validate']:\n"
            f"    if {validation_exit}:\n"
            "        print('snapshot metadata is corrupt', file=sys.stderr)\n"
            f"        raise SystemExit({validation_exit})\n"
            "    print(json.dumps({'valid': True, 'missingFiles': []}))\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['snapshot', 'export']:\n"
            "    snapshot = pathlib.Path(args[2])\n"
            "    snapshot.mkdir(parents=True)\n"
            "    (snapshot / 'cache.db').write_bytes(b'fixture')\n"
            "    print(json.dumps({'path': str(snapshot)}))\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['cache', 'status']:\n"
            "    cache_dir = pathlib.Path(os.environ['XDG_CACHE_HOME']) / 'hlx' / 'public'\n"
            "    print('Cache status (public (unauthenticated)):')\n"
            "    print(f'  Cache dir:       {cache_dir}')\n"
            "    print('  Metadata entries: 2')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(9)\n"
        )
        executable.chmod(0o755)
        return executable, calls

    def test_accepts_exact_pinned_hlx(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(temporary, observer_collect.HLX_VERSION)
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )
            provider._collect_selected_builds = lambda selected: None

            provider.prepare([{"id": 1622443}])

        self.assertEqual(provider.version, "0.11.0")
        self.assertEqual(provider.metadata["commit"], observer_collect.HLX_COMMIT)
        self.assertEqual(
            provider.metadata["collectionPolicy"]["submissionLogs"],
            "full-successful-submissions",
        )

    def test_build_listing_filters_results_locally_and_records_bound(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        builds = [
            build(101, "2026-10-05T10:00:00Z"),
            {
                **build(100, "2026-10-05T09:00:00Z"),
                "result": "succeeded",
            },
            {
                **build(99, "2026-10-05T08:00:00Z"),
                "result": "partiallySucceeded",
            },
        ]
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
                builds,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )

            listing = provider.list_builds(154, 10)

        self.assertTrue(listing.complete)
        self.assertEqual(
            [item["id"] for item in listing.value["value"]],
            [101, 99],
        )
        self.assertEqual(listing.identity["queryTop"], 100)
        self.assertEqual(listing.identity["returnedBuildCount"], 3)
        self.assertEqual(listing.identity["locallyMatchedResultCount"], 2)
        self.assertFalse(listing.identity["continuationSupported"])

    def test_missing_build_result_cannot_become_empty(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        malformed = build(101, "2026-10-05T10:00:00Z")
        del malformed["result"]
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(temporary, observer_collect.HLX_VERSION, [malformed])
            provider = observer_collect.HlxV011Provider(executable, Path(temporary) / "output")
            with self.assertRaises(observer_collect.CollectorError) as raised:
                provider.list_builds(154, 10)
        self.assertEqual(raised.exception.code, "invalid_evidence")

    def test_saturated_build_bound_without_ten_failures_is_incomplete(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        builds = []
        for index in range(observer_collect.HLX_BUILD_QUERY_LIMIT):
            item = build(
                1000 - index,
                (NOW - dt.timedelta(minutes=index + 1))
                .isoformat()
                .replace("+00:00", "Z"),
            )
            if index >= 9:
                item["result"] = "succeeded"
            builds.append(item)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
                builds,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
                as_of=NOW,
            )

            listing = provider.list_builds(154, 10)

        self.assertFalse(listing.complete)
        self.assertTrue(listing.identity["bounded"])
        self.assertEqual(listing.identity["locallyMatchedResultCount"], 9)

    def test_build_listing_accepts_azdo_value_wrapper(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        builds = [build(101, "2026-10-05T10:00:00Z")]
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
                builds,
                wrap_builds=True,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
                as_of=NOW,
            )

            listing = provider.list_builds(154, 10)

        self.assertTrue(listing.complete)
        self.assertEqual(listing.value["value"][0]["id"], 101)

    def test_selected_builds_share_cache_and_export_only_final_snapshot(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable, calls_path = self._fake_collecting_hlx(temporary)
            output = Path(temporary) / "output"
            provider = observer_collect.HlxV011Provider(executable, output)
            selected = [
                {"id": 100, "role": "source"},
                {"id": 99, "role": "history"},
            ]

            provider._collect_selected_builds(selected)
            snapshot = provider.finalize(complete=True)
            calls = [
                json.loads(line)
                for line in calls_path.read_text().splitlines()
            ]

        collect_calls = [call for call in calls if call[:2] == ["collect", "azdo-build"]]
        self.assertEqual(len(collect_calls), 2)
        self.assertEqual(
            collect_calls[0][collect_calls[0].index("--cache-dir") + 1],
            collect_calls[1][collect_calls[1].index("--cache-dir") + 1],
        )
        self.assertNotIn("--export", collect_calls[0])
        self.assertNotIn("--export", collect_calls[1])
        self.assertEqual(
            collect_calls[0][collect_calls[0].index("--log-scope") + 1],
            "none",
        )
        self.assertEqual(
            collect_calls[0][collect_calls[0].index("--helix-scope") + 1],
            "none",
        )
        self.assertIn(["snapshot", "export", str(output / "hlx-snapshot")], calls)
        self.assertIn(
            ["snapshot", "validate", str(output / "hlx-snapshot")],
            calls,
        )
        self.assertEqual(snapshot["path"], "hlx-snapshot")
        self.assertEqual(snapshot["cachePath"], "xdg-cache/hlx")
        self.assertEqual(
            snapshot["cacheStatus"]["effectiveCacheDir"],
            "xdg-cache/hlx/public",
        )
        self.assertTrue(snapshot["cacheStatus"]["verified"])
        self.assertTrue(snapshot["complete"])
        self.assertEqual(
            [item["buildId"] for item in snapshot["collections"]],
            [100, 99],
        )
        self.assertTrue(snapshot["validation"]["valid"])

    def test_live_work_item_inventory_requires_declared_complete_count(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )
            provider._run_json = lambda arguments, operation: {
                "totalWorkItems": 3,
                "failed": [{"Name": "failed", "ExitCode": 1}],
                "passed": [{"Name": "passed", "ExitCode": 0}],
            }

            with self.assertRaises(observer_collect.CollectorError) as raised:
                provider.helix_work_items(JOB_1)

        self.assertEqual(raised.exception.code, "incomplete_helix_inventory")
        self.assertEqual(raised.exception.scope["returnedWorkItems"], 2)

    def test_live_work_item_inventory_records_complete_provenance(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )
            provider._run_json = lambda arguments, operation: {
                "totalWorkItems": 2,
                "failed": [{"Name": "failed", "ExitCode": 1}],
                "passed": [{"Name": "passed", "ExitCode": 0}],
            }

            inventory = provider.helix_work_items(JOB_1)

        self.assertTrue(inventory.complete)
        self.assertEqual(len(inventory.value), 2)
        self.assertEqual(inventory.identity["source"], "live-provider")
        self.assertEqual(inventory.identity["totalWorkItems"], 2)

    def test_live_mcp_console_returns_full_text(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )
            calls = []

            def mcp_call(tool, arguments):
                calls.append((tool, arguments))
                return {"content": [{"type": "text", "text": "full console"}]}

            provider._mcp_call = mcp_call
            console_evidence = provider.helix_console(JOB_1, "failed / name?#")

        self.assertEqual(
            calls,
            [
                (
                    "helix_logs",
                    {"jobId": JOB_1, "workItem": "failed / name?#", "tail": None},
                )
            ],
        )
        self.assertEqual(console_evidence.text, "full console")
        self.assertEqual(console_evidence.outcome, "ok")
        self.assertEqual(
            console_evidence.identity["url"],
            f"https://helix.dot.net/api/2019-06-17/jobs/{JOB_1}/workitems/"
            "failed%20%2F%20name%3F%23/console",
        )
        self.assertEqual(console_evidence.identity["urlKind"], "canonical-console-reference")

    def test_live_mcp_console_maps_only_canonical_404_to_retention(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )
            provider._mcp_call = lambda tool, arguments: {
                "isError": True,
                "structuredContent": {
                    "error": {
                        "kind": "not_found",
                        "httpStatus": 404,
                        "message": "canonical console returned 404",
                    }
                },
            }

            console_evidence = provider.helix_console(JOB_1, "rolled-off")

        self.assertEqual(console_evidence.outcome, "console_404")
        self.assertIn("404", console_evidence.detail)

    def test_live_mcp_console_rejects_non_404_provider_error(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )
            provider._mcp_call = lambda tool, arguments: {
                "isError": True,
                "structuredContent": {
                    "error": {
                        "kind": "unauthorized",
                        "httpStatus": 401,
                        "message": "credentials unavailable",
                    }
                },
            }

            with self.assertRaises(observer_collect.CollectorError) as raised:
                provider.helix_console(JOB_1, "failed")

        self.assertEqual(raised.exception.code, "helix_console_unavailable")
        self.assertEqual(
            raised.exception.scope["providerError"]["kind"],
            "unauthorized",
        )

    def test_snapshot_export_failure_is_fatal(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(
                temporary,
                observer_collect.HLX_VERSION,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )

            with self.assertRaises(observer_collect.CollectorError) as raised:
                provider.finalize(complete=True)

        self.assertEqual(raised.exception.code, "hlx_snapshot_export_failed")
        self.assertEqual(raised.exception.scope["exitCode"], 9)

    def test_snapshot_validation_failure_is_fatal(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable, _ = self._fake_collecting_hlx(
                temporary,
                validation_exit=1,
            )
            provider = observer_collect.HlxV011Provider(
                executable,
                Path(temporary) / "output",
            )

            with self.assertRaises(observer_collect.CollectorError) as raised:
                provider.finalize(complete=True)

        self.assertEqual(raised.exception.code, "hlx_snapshot_validation_failed")
        self.assertEqual(raised.exception.scope["exitCode"], 1)
        self.assertIn("corrupt", str(raised.exception))

    def test_rejects_unpinned_hlx_version(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temporary:
            executable = self._fake_hlx(temporary, "0.11.1")

            with self.assertRaises(observer_collect.CollectorError) as raised:
                observer_collect.HlxV011Provider(
                    executable,
                    Path(temporary) / "output",
                )

        self.assertEqual(raised.exception.code, "hlx_version_mismatch")
        self.assertEqual(raised.exception.scope["expectedVersion"], "0.11.0")
        self.assertEqual(raised.exception.scope["actualVersion"], "0.11.1")


if __name__ == "__main__":
    unittest.main()
