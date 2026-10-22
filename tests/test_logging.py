"""日志脱敏测试。"""

from __future__ import annotations

import logging
from pathlib import Path

from aimovie.logging_setup import RedactingFormatter, setup_logging
from aimovie.utils import redact_secrets


def test_redact_secrets_patterns() -> None:
    assert "sk-abcdefgh12345" not in redact_secrets("key=sk-abcdefgh12345")
    assert "***REDACTED***" in redact_secrets("Authorization: Bearer abcdefgh123456")
    assert "***REDACTED***" in redact_secrets("api_key=abcd1234efgh")


def test_log_file_redacts_api_key(tmp_path: Path) -> None:
    log_path = setup_logging(tmp_path / "logs", "INFO", console=False)
    secret = "sk-1234567890abcdef"
    logging.getLogger("test.secret").warning("调用接口使用 Key：%s", secret)
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_path.read_text(encoding="utf-8")
    assert secret not in text
    assert "***REDACTED***" in text


def test_formatter_redacts_traceback_text() -> None:
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="t",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Authorization: Bearer abcdefgh12345678",
        args=(),
        exc_info=None,
    )
    assert "abcdefgh12345678" not in formatter.format(record)
