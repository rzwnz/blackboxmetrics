import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_module(name: str, relative_path: str):
    project_root = Path(__file__).resolve().parents[1]
    module_path = project_root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def install_s3_import_stubs():
    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")
        boto3_mod.client = lambda *args, **kwargs: None
        sys.modules["boto3"] = boto3_mod

    if "botocore" not in sys.modules:
        sys.modules["botocore"] = types.ModuleType("botocore")

    if "botocore.config" not in sys.modules:
        config_mod = types.ModuleType("botocore.config")

        class DummyConfig:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        config_mod.Config = DummyConfig
        sys.modules["botocore.config"] = config_mod

    if "botocore.exceptions" not in sys.modules:
        exceptions_mod = types.ModuleType("botocore.exceptions")

        class BotoCoreError(Exception):
            pass

        class ClientError(Exception):
            pass

        exceptions_mod.BotoCoreError = BotoCoreError
        exceptions_mod.ClientError = ClientError
        sys.modules["botocore.exceptions"] = exceptions_mod


class ExporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("prometheus_client") is None:
            raise unittest.SkipTest("prometheus_client is not installed in local environment")

        install_s3_import_stubs()
        cls.s3 = load_module("s3_exporter", "exporters/s3-exporter/s3_exporter.py")
        cls.tomcat = load_module("tomcat_exporter", "exporters/tomcat-exporter/tomcat_exporter.py")

    def test_tomcat_parse_status_updates_metrics(self):
        xml = """
        <status>
          <jvm>
            <memory free=\"10\" total=\"20\" max=\"30\"/>
          </jvm>
          <connector name=\"http-nio-8080\">
            <threadInfo maxThreads=\"200\" currentThreadCount=\"10\" currentThreadsBusy=\"5\"/>
            <requestInfo requestCount=\"100\" errorCount=\"2\" bytesReceived=\"1000\" bytesSent=\"2000\" processingTime=\"150\" maxTime=\"20\"/>
          </connector>
        </status>
        """

        self.tomcat._parse_status(xml)

        self.assertEqual(self.tomcat.jvm_memory_free_bytes._value.get(), 10)
        self.assertEqual(self.tomcat.jvm_memory_total_bytes._value.get(), 20)
        self.assertEqual(self.tomcat.jvm_memory_max_bytes._value.get(), 30)
        self.assertEqual(self.tomcat.connector_thread_busy.labels(connector="http-nio-8080")._value.get(), 5)
        self.assertEqual(self.tomcat.connector_request_count_total.labels(connector="http-nio-8080")._value.get(), 100)

    def test_s3_collect_bucket_metrics_updates_gauges(self):
        class FakePaginator:
            def paginate(self, Bucket):
                return [
                    {"Contents": [{"Size": 5}, {"Size": 7}]}
                ]

        class FakeClient:
            def get_paginator(self, _name):
                return FakePaginator()

        self.s3._collect_bucket_metrics(FakeClient(), "bucket-a")

        self.assertEqual(self.s3.bucket_objects_total.labels(bucket="bucket-a")._value.get(), 2)
        self.assertEqual(self.s3.bucket_size_bytes.labels(bucket="bucket-a")._value.get(), 12)
        self.assertEqual(self.s3.bucket_largest_object_bytes.labels(bucket="bucket-a")._value.get(), 7)
        self.assertEqual(self.s3.bucket_up.labels(bucket="bucket-a")._value.get(), 1)


if __name__ == "__main__":
    unittest.main()
