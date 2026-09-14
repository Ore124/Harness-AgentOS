import logging
import unittest

from logger import EncodingSafeStreamHandler, HarnessFormatter


class _GBKStream:
    encoding = "gbk"

    def __init__(self):
        self.value = ""

    def write(self, value):
        value.encode(self.encoding)
        self.value += value

    def flush(self):
        return None


class EncodingSafeStreamHandlerTests(unittest.TestCase):
    def test_unencodable_log_markers_do_not_raise_or_emit_logging_errors(self):
        stream = _GBKStream()
        handler = EncodingSafeStreamHandler(stream)
        handler.setFormatter(HarnessFormatter())

        logger = logging.Logger("encoding-test")
        logger.addHandler(handler)
        logger.warning("warning with emoji ⚠")

        self.assertIn("warning with emoji", stream.value)
        self.assertNotIn("⚠", stream.value)


if __name__ == "__main__":
    unittest.main()
