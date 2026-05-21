"""Tests for agent_sandbox_benchmark."""

import json
from unittest import mock

from absl.testing import absltest
from absl.testing import flagsaver
from perfkitbenchmarker.linux_benchmarks import agent_sandbox_benchmark
from tests import pkb_common_test_case


class GetConfigTest(pkb_common_test_case.PkbCommonTestCase):

  def testGetConfigHasContainerCluster(self):
    # LoadConfig returns the unwrapped inner config dict.
    config = agent_sandbox_benchmark.GetConfig({})
    self.assertIn('container_cluster', config)

  def testGetConfigHasDescription(self):
    config = agent_sandbox_benchmark.GetConfig({})
    self.assertIn('description', config)

  def testGetConfigVmCount(self):
    config = agent_sandbox_benchmark.GetConfig({})
    self.assertEqual(config['container_cluster']['vm_count'], 3)


class CheckPrerequisitesTest(pkb_common_test_case.PkbCommonTestCase):

  def testPassesWithDefaultFlags(self):
    agent_sandbox_benchmark.CheckPrerequisites({})

  @flagsaver.flagsaver(agent_sandbox_qps=0.0)
  def testFailsWithZeroQPS(self):
    with self.assertRaises(Exception):
      agent_sandbox_benchmark.CheckPrerequisites({})

  @flagsaver.flagsaver(agent_sandbox_qps=-1.0)
  def testFailsWithNegativeQPS(self):
    with self.assertRaises(Exception):
      agent_sandbox_benchmark.CheckPrerequisites({})

  @flagsaver.flagsaver(agent_sandbox_total=0)
  def testFailsWithZeroTotal(self):
    with self.assertRaises(Exception):
      agent_sandbox_benchmark.CheckPrerequisites({})

  @flagsaver.flagsaver(agent_sandbox_warmpool_size=-1)
  def testFailsWithNegativeWarmPool(self):
    with self.assertRaises(Exception):
      agent_sandbox_benchmark.CheckPrerequisites({})


class ComputeMetricsTest(pkb_common_test_case.PkbCommonTestCase):

  def _record(self, **overrides):
    base = {
        'claim_name': 'claim-0',
        'claim_requested_at': 1000.0,
        'claim_ready_at': 1012.0,
        'exec_started_at': 1012.5,
        'exec_completed_at': 1032.5,
        'released_at': 1033.0,
        'error': None,
        'claim_ready_at_utc': None,
        'pod_name': None,
        'pod_node_name': None,
        'pod_created_at_utc': 1001.0,
        'pod_scheduled_at_utc': 1002.0,
        'pod_initialized_at_utc': None,
        'pod_containers_ready_at_utc': 1010.0,
        'pod_ready_at_utc': 1011.0,
        'pod_age_at_bind_s': -1.0,
    }
    base.update(overrides)
    return base

  def testStartupTime(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['startup_time_s'], 12.0)

  def testTimeToFirstExec(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['time_to_first_exec_s'], 12.5)

  def testExecDuration(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['exec_duration_s'], 20.0)

  def testTotalLifecycle(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['total_lifecycle_s'], 33.0)

  def testPodStartup(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['pod_startup_s'], 10.0)

  def testCreateToSchedule(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['create_to_schedule_s'], 1.0)

  def testScheduleToContainersReady(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['schedule_to_containers_ready_s'], 8.0)

  def testPodAgeAtBind(self):
    metrics = agent_sandbox_benchmark._ComputeMetrics(self._record())
    self.assertAlmostEqual(metrics['pod_age_at_bind_s'], -1.0)

  def testErrorRecordReturnsEmpty(self):
    record = self._record(error='RuntimeError: something failed')
    metrics = agent_sandbox_benchmark._ComputeMetrics(record)
    self.assertEmpty(metrics)

  def testMissingTimestampSkipsMetric(self):
    record = self._record(claim_ready_at=None)
    metrics = agent_sandbox_benchmark._ComputeMetrics(record)
    self.assertNotIn('startup_time_s', metrics)
    # Other metrics should still be computed.
    self.assertIn('total_lifecycle_s', metrics)


class ParseLoadRunnerOutputTest(pkb_common_test_case.PkbCommonTestCase):

  def _make_jsonl(self, records):
    return '\n'.join(json.dumps(r) for r in records) + '\n'

  def _record(self, req=1000.0, ready=1012.0, exec_start=1012.5,
              exec_end=1032.5, released=1033.0, error=None):
    return {
        'claim_name': 'claim-0',
        'claim_requested_at': req,
        'claim_ready_at': ready,
        'exec_started_at': exec_start,
        'exec_completed_at': exec_end,
        'released_at': released,
        'error': error,
        'claim_ready_at_utc': None,
        'pod_name': None,
        'pod_node_name': None,
        'pod_created_at_utc': None,
        'pod_scheduled_at_utc': None,
        'pod_initialized_at_utc': None,
        'pod_containers_ready_at_utc': None,
        'pod_ready_at_utc': None,
        'pod_age_at_bind_s': None,
    }

  def testEmptyInputReturnsNoSamples(self):
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput('', {})
    self.assertEmpty(samples)

  def testSingleSuccessfulRecord(self):
    jsonl = self._make_jsonl([self._record()])
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(
        jsonl, {'qps': 1.0}
    )
    self.assertNotEmpty(samples)
    metric_names = {s.metric for s in samples}
    self.assertIn('startup_time_s_p50', metric_names)
    self.assertIn('total_lifecycle_s_p50', metric_names)

  def testSkipsNonJsonLines(self):
    content = 'Starting load runner...\n' + self._make_jsonl([self._record()])
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(content, {})
    self.assertNotEmpty(samples)

  def testSkipsErrorRecords(self):
    jsonl = self._make_jsonl([
        self._record(error='RuntimeError: failed'),
    ])
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(jsonl, {})
    self.assertEmpty(samples)

  def testMetadataAttachedToSamples(self):
    jsonl = self._make_jsonl([self._record()])
    meta = {'qps': 2.0, 'total': 10}
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(jsonl, meta)
    for s in samples:
      self.assertEqual(s.metadata['qps'], 2.0)
      self.assertEqual(s.metadata['total'], 10)

  def testPercentilesProducedForEachMetric(self):
    records = [self._record(req=1000.0 + i) for i in range(10)]
    jsonl = self._make_jsonl(records)
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(jsonl, {})
    metric_names = {s.metric for s in samples}
    # startup_time_s is constant across records so all percentiles equal.
    self.assertIn('startup_time_s_p50', metric_names)
    self.assertIn('startup_time_s_p90', metric_names)
    self.assertIn('startup_time_s_p95', metric_names)
    self.assertIn('startup_time_s_p99', metric_names)

  def testSampleCountInMetadata(self):
    records = [self._record() for _ in range(5)]
    jsonl = self._make_jsonl(records)
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(jsonl, {})
    startup_samples = [
        s for s in samples if s.metric == 'startup_time_s_p50'
    ]
    self.assertLen(startup_samples, 1)
    self.assertEqual(startup_samples[0].metadata['sample_count'], 5)

  def testUnitsAreSeconds(self):
    jsonl = self._make_jsonl([self._record()])
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(jsonl, {})
    for s in samples:
      self.assertEqual(s.unit, 'seconds')

  def testP50ValueCorrect(self):
    # 10 records with startup_time_s = 12.0 each (constant — ready tracks req).
    records = [self._record(req=1000.0 + i, ready=1012.0 + i) for i in range(10)]
    jsonl = self._make_jsonl(records)
    samples = agent_sandbox_benchmark._ParseLoadRunnerOutput(jsonl, {})
    p50 = next(s for s in samples if s.metric == 'startup_time_s_p50')
    # startup = ready - req = 12.0 for all records.
    self.assertAlmostEqual(p50.value, 12.0)


if __name__ == '__main__':
  absltest.main()
