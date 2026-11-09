"""The operator log must never carry a vendor API key. httpx logs full
request URLs at INFO, and Finnhub/FMP put the key in the query string."""

import logging

from midas import cli


def test_monitor_logging_keeps_request_urls_out_of_the_log():
    cli.configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
