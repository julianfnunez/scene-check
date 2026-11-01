import logging

import pytest

from logging_setup import PollingFilter, configure_logging


@pytest.mark.parametrize('method,path,status,visible', [
    ('GET', '/api/scenes', 200, False),
    ('GET', '/api/checks', 200, False),
    ('GET', '/api/checks?limit=10', 200, False),
    ('GET', '/api/checks/abc', 200, False),
    ('GET', '/api/scenes', 500, True),
    ('GET', '/api/checks', 403, True),
    ('GET', '/api/checks/abc', 404, True),
    ('POST', '/api/checks/active', 202, True),
    ('GET', '/', 200, True),
])
def test_only_successful_polling_is_quiet(method, path, status, visible):
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 1,
                              '%s %s %s %s %s', ('client', method, path, '1.1', status), None)
    assert PollingFilter().filter(record) is visible


def test_unrecognized_access_records_are_kept():
    record = logging.LogRecord('uvicorn.access', logging.ERROR, '', 1, 'Server error', (), None)
    assert PollingFilter().filter(record)


def test_logging_setup_is_repeatable():
    configure_logging()
    configure_logging()
    access = logging.getLogger('uvicorn.access')
    assert sum(isinstance(f, PollingFilter) for f in access.filters) == 1
    assert logging.getLogger('mcp.server.lowlevel.server').getEffectiveLevel() == logging.WARNING
