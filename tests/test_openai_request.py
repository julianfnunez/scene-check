import asyncio
import json

import httpx
import pytest

import agent


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret')
    calls, waits = [], []
    client_class = httpx.AsyncClient

    def install(outcomes):
        outcomes = iter(outcomes)

        async def handle(request):
            calls.append(json.loads(request.content))
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(agent.httpx, 'AsyncClient',
                            lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(agent.asyncio, 'sleep', sleep)
    monkeypatch.setattr(agent.random, 'uniform', lambda *args: .25)
    return install, calls, waits


def success():
    return httpx.Response(200, json={'status': 'completed', 'output': []})


@pytest.mark.parametrize('failure', [
    *[httpx.Response(code) for code in (408, 409, 429, 500, 502, 503, 504)],
    httpx.ConnectError('test-secret'), httpx.ReadTimeout('test-secret'),
    httpx.RemoteProtocolError('test-secret'),
])
def test_transient_failure_recovers(transport, failure):
    install, calls, waits = transport
    install([failure, success()])
    payload = {'model': 'test', 'store': False, 'input': 'same evidence'}
    assert asyncio.run(agent.request(payload))['status'] == 'completed'
    assert calls == [payload, payload]
    assert waits == [1.25]


def test_retry_limit_and_safe_errors(transport, caplog):
    install, calls, waits = transport
    install([httpx.ConnectError('test-secret sensitive evidence')] * 3)
    with pytest.raises(RuntimeError, match='retry limit reached') as exc:
        asyncio.run(agent.request({}))
    assert len(calls) == 3
    assert waits == [1.25, 2.25]
    assert 'test-secret' not in str(exc.value) + caplog.text
    assert 'sensitive evidence' not in str(exc.value) + caplog.text


@pytest.mark.parametrize('response', [
    *[httpx.Response(code, text='test-secret') for code in (400, 401, 403, 404, 422)],
    httpx.Response(429, json={'error': {'code': 'insufficient_quota'}}),
    httpx.Response(200, text='not json test-secret'),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={'status': 'incomplete'}),
])
def test_permanent_or_invalid_responses_do_not_retry(transport, response):
    install, calls, waits = transport
    install([response])
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(agent.request({}))
    assert len(calls) == 1
    assert waits == []
    assert 'test-secret' not in str(exc.value)


@pytest.mark.parametrize('headers,delay', [
    ({'Retry-After': '3'}, 3),
    ({'retry-after-ms': '1250'}, 1.25),
    ({'Retry-After': 'invalid'}, 1.25),
    ({'Retry-After': '-1'}, 1.25),
    ({'Retry-After': 'NaN'}, 1.25),
    ({'Retry-After': 'inf'}, 1.25),
])
def test_server_retry_delay(transport, headers, delay):
    install, _, waits = transport
    install([httpx.Response(429, headers=headers), success()])
    asyncio.run(agent.request({}))
    assert waits == [delay]


def test_long_server_delay_stops_without_retry(transport):
    install, calls, waits = transport
    install([httpx.Response(503, headers={'Retry-After': '3600'})])
    with pytest.raises(RuntimeError, match='time budget'):
        asyncio.run(agent.request({}))
    assert len(calls) == 1
    assert waits == []


def test_cancellation_propagates(transport):
    install, calls, waits = transport
    install([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.request({}))
    assert len(calls) == 1
    assert waits == []


def test_total_deadline(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret')
    monkeypatch.setattr(agent, 'REQUEST_BUDGET_SECONDS', 0)
    async def slow_post(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(httpx.AsyncClient, 'post', slow_post)
    with pytest.raises(RuntimeError, match='time budget'):
        asyncio.run(agent.request({}))


def test_http_date_retry_delay():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    assert 18 <= agent.retry_delay({'retry-after': value}, 0) <= 20


def test_missing_key_does_not_send(transport, monkeypatch):
    install, calls, _ = transport
    install([])
    monkeypatch.delenv('OPENAI_API_KEY')
    with pytest.raises(RuntimeError, match='Set OPENAI_API_KEY'):
        asyncio.run(agent.request({}))
    assert calls == []
