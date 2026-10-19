"""
test_telemetry.py - Unit Tests for System Resource Telemetry & Stage Stopwatch
"""

import time
import unittest
from telemetry import (
    get_system_metrics,
    StageStopwatch,
    record_high_res_verification,
    get_high_res_verification_telemetry,
)


class TestTelemetry(unittest.TestCase):

    def test_get_system_metrics_structure(self):
        """Verifies that get_system_metrics returns complete, valid structure."""
        metrics = get_system_metrics()
        self.assertIn("cpu", metrics)
        self.assertIn("ram", metrics)
        self.assertIn("gpu", metrics)
        self.assertIn("timestamp", metrics)

        # CPU validation
        cpu = metrics["cpu"]
        self.assertIn("percent", cpu)
        self.assertGreaterEqual(cpu["logical_cores"], 1)
        self.assertGreaterEqual(cpu["physical_cores"], 1)

        # RAM validation
        ram = metrics["ram"]
        self.assertGreater(ram["total_gb"], 0.0)
        self.assertGreater(ram["used_gb"], 0.0)
        self.assertGreaterEqual(ram["percent"], 0.0)
        self.assertLessEqual(ram["percent"], 100.0)

        # GPU validation
        gpu = metrics["gpu"]
        self.assertIn("has_gpu", gpu)
        self.assertIn("name", gpu)
        self.assertIn("gpu_util_percent", gpu)
        self.assertIn("vram_used_gb", gpu)
        self.assertIn("vram_total_gb", gpu)

    def test_stage_stopwatch_lifecycle(self):
        """Verifies that StageStopwatch accurately measures and aggregates stages."""
        sw = StageStopwatch()
        sw.start_total()

        sw.start_stage("Parsing")
        time.sleep(0.04)
        elapsed_parsing = sw.stop_stage()
        self.assertGreaterEqual(elapsed_parsing, 0.03)

        sw.start_stage("LLM Inference")
        time.sleep(0.04)
        sw.start_stage("Normalization")  # Implicitly closes LLM Inference
        time.sleep(0.02)
        total_time = sw.finish_total()

        data = sw.to_dict()
        self.assertIn("stages", data)
        self.assertIn("total_seconds", data)

        stages = data["stages"]
        self.assertIn("Parsing", stages)
        self.assertIn("LLM Inference", stages)
        self.assertIn("Normalization", stages)
        self.assertGreaterEqual(data["total_seconds"], 0.09)

    def test_stage_stopwatch_empty_behavior(self):
        """Verifies that an unstarted stopwatch produces valid zero outputs without exceptions."""
        sw = StageStopwatch()
        total = sw.finish_total()
        self.assertEqual(total, 0.0)
        data = sw.to_dict()
        self.assertEqual(data["stages"], {})
        self.assertEqual(data["total_seconds"], 0.0)

    def test_record_and_get_high_res_verification(self):
        """Verifies recording and retrieving telemetry for high_res_verification_pass."""
        rec = record_high_res_verification(
            elapsed_seconds=1.85,
            memory_mb=42.50,
            pages_count=2,
            stage_name="high_res_verification_pass"
        )
        self.assertEqual(rec["metric_name"], "high_res_verification_pass")
        self.assertEqual(rec["elapsed_seconds"], 1.85)
        self.assertEqual(rec["memory_mb"], 42.50)
        self.assertEqual(rec["pages_count"], 2)

        stored = get_high_res_verification_telemetry("high_res_verification_pass")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["elapsed_seconds"], 1.85)
        self.assertEqual(stored["pages_count"], 2)


if __name__ == "__main__":
    unittest.main()
