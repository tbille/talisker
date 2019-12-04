#
# Copyright (c) 2015-2018 Canonical, Ltd.
#
# This file is part of Talisker
# (see http://github.com/canonical-ols/talisker).
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

from builtins import *  # noqa

from datetime import datetime, timedelta
import json
import sys
import time
import wsgiref.util

import pytest
from freezegun import freeze_time

from talisker import wsgi, Context
from talisker.util import datetime_to_timestamp
import talisker.sentry


@pytest.fixture
def start_response():
    def mock_start_response(status, headers, exc_info=None):
        mock_start_response.status = status
        mock_start_response.exc_info = exc_info
        mock_start_response.headers = headers
        mock_start_response.body = body = []

        # this mimics expected WSGI server behaviour
        if exc_info and mock_start_response.headers_sent:
            raise exc_info[0].with_traceback(exc_info[1], exc_info[2])

        return lambda x: body.append(x)

    # this mimics WSGI server state
    mock_start_response.headers_sent = False

    return mock_start_response


@pytest.fixture
def run_wsgi(wsgi_env, start_response):
    "Fixture for running a request through the wsgi stack."

    def run(env=None, status='200 OK', headers=None, body=None, duration=1):
        if env:
            wsgi_env.update(env)
        if body is None:
            body = [b'0' * 1000]

        with freeze_time() as frozen:
            wsgi_env['start_time'] = time.time()
            request = wsgi.TaliskerWSGIRequest(wsgi_env, start_response, [])
            frozen.tick(duration)
            request.start_response(status, headers or [], None)
            iter = request.wrap_response(body)
            response = list(iter)
            iter.close()

        return start_response.headers, response

    return run


def test_error_response_handler(wsgi_env):
    wsgi_env['REQUEST_ID'] = 'REQUESTID'
    wsgi_env['SENTRY_ID'] = 'SENTRYID'
    wsgi_env['HTTP_ACCEPT'] = 'application/json'
    headers = [('X-VCS-Revision', 'revid')]
    exc_info = None

    try:
        raise Exception('test')
    except Exception:
        exc_info = sys.exc_info()

    content_type, body = wsgi.talisker_error_response(
        wsgi_env,
        headers,
        exc_info,
    )
    error = json.loads(body.decode('utf8'))
    assert content_type == 'application/json'
    assert error['title'] == 'Request REQUESTID: Exception'
    assert error['id'] == {
        'Request-Id': 'REQUESTID',
        'Sentry-ID': 'SENTRYID',
    }
    assert error['traceback'] == '[traceback hidden]'
    assert error['request_headers'] == {
        'Accept': 'application/json',
        'Host': '127.0.0.1',
    }
    assert error['wsgi_env']['REQUEST_ID'] == 'REQUESTID'
    assert error['wsgi_env']['SENTRY_ID'] == 'SENTRYID'
    assert error['response_headers'] == {
        'X-VCS-Revision': 'revid',
    }


def test_error_response_handler_devel(wsgi_env, config):
    config['DEVEL'] = '1'
    wsgi_env['REQUEST_ID'] = 'REQUESTID'
    wsgi_env['SENTRY_ID'] = 'SENTRYID'
    wsgi_env['HTTP_ACCEPT'] = 'application/json'
    headers = [('X-VCS-Revision', 'revid')]
    exc_info = None

    try:
        raise Exception('test')
    except Exception:
        exc_info = sys.exc_info()

    content_type, body = wsgi.talisker_error_response(
        wsgi_env,
        headers,
        exc_info,
    )
    error = json.loads(body.decode('utf8'))
    assert error['title'] == 'Request REQUESTID: test'
    assert error['traceback'][0] == 'Traceback (most recent call last):'
    assert error['traceback'][-3] == '    raise Exception(\'test\')'
    assert error['traceback'][-2] == 'Exception: test'


def test_wsgi_request_start_response(wsgi_env, start_response):
    wsgi_env['REQUEST_ID'] = 'ID'
    headers = {'HEADER': 'VALUE'}
    request = wsgi.TaliskerWSGIRequest(wsgi_env, start_response, headers)
    request.start_response('200 OK', [], None)
    request.call_start_response()
    assert request.status_code == 200
    assert start_response.status == request.status == '200 OK'
    assert start_response.headers == request.headers == [
        ('HEADER', 'VALUE'),
        ('X-Request-Id', 'ID'),
    ]
    assert start_response.exc_info is request.exc_info is None


def test_wsgi_request_soft_timeout_default(run_wsgi, context):
    run_wsgi()
    assert context.sentry == []


@pytest.mark.skipif(not talisker.sentry.enabled, reason='need raven installed')
def test_wsgi_request_soft_explicit(run_wsgi, context):
    talisker.Context.current.soft_timeout = 100
    run_wsgi(duration=2)
    msg = context.sentry[0]
    assert msg['message'] == 'start_response over soft timeout: 100ms'
    assert msg['level'] == 'warning'


def test_wsgi_request_wrap_response(run_wsgi, context):
    headers, body = run_wsgi(body=[b'output', b' ', b'here'])
    output = b''.join(body)
    assert output == b'output here'
    context.assert_log(
        msg='GET /',
        extra=dict([
            ('method', 'GET'),
            ('path', '/'),
            ('status', 200),
            ('duration_ms', 1000.0),
            ('ip', '127.0.0.1'),
            ('proto', 'HTTP/1.0'),
            ('length', len(output)),
        ]),
    )


def test_wsgi_request_wrap_file(run_wsgi, context, tmpdir):
    path = tmpdir.join('filecontent')
    path.write('CONTENT')
    wrapper = wsgiref.util.FileWrapper(open(str(path)))
    env = {'wsgi.file_wrapper': wsgiref.util.FileWrapper}

    headers, body = run_wsgi(env, body=wrapper)
    output = ''.join(body)

    assert output == 'CONTENT'
    context.assert_log(
        msg='GET /',
        extra=dict([
            ('method', 'GET'),
            ('path', '/'),
            ('status', 200),
            ('duration_ms', 1000.0),
            ('ip', '127.0.0.1'),
            ('proto', 'HTTP/1.0'),
            ('length', len(output)),
            ('filepath', str(path)),
        ]),
    )


def test_wsgi_request_wrap_error(run_wsgi, context):
    env = {
        'REQUEST_ID': 'REQUESTID',
        'HTTP_ACCEPT': 'application/json',
    }

    class ErrorGenerator():
        def __iter__(self):
            return self

        def __next__(self):
            raise Exception('error')

    headers, body = run_wsgi(env, body=ErrorGenerator())
    output = b''.join(body)
    error = json.loads(output.decode('utf8'))

    assert error['title'] == 'Request REQUESTID: Exception'

    context.assert_log(
        msg='GET /',
        extra=dict([
            ('method', 'GET'),
            ('path', '/'),
            ('status', 500),
            ('duration_ms', 1000.0),
            ('ip', '127.0.0.1'),
            ('proto', 'HTTP/1.0'),
            ('length', len(output)),
            ('exc_type', 'Exception'),
        ]),
    )


def test_wsgi_request_wrap_error_headers_sent(run_wsgi, context):

    def iterator():
        start_response.headers_sent = True
        yield b'some content'
        raise Exception('error')

    with pytest.raises(Exception):
        run_wsgi(body=iterator())


def test_wsgi_request_wrap_no_body(run_wsgi, context):
    def iterator():
        return []

    headers, body = run_wsgi(status='304 Not Modified', body=iterator())

    output = b''.join(body)
    assert output == b''
    assert headers == []


def test_wsgi_request_log(run_wsgi, context):
    env = {
        'PATH_INFO': '/foo',
        'QUERY_STRING': 'bar=baz',
        'HTTP_X_FORWARDED_FOR': '203.0.113.195, 150.172.238.178',
        'CONTENT_LENGTH': '100',
        'CONTENT_TYPE': 'application/json',
        'HTTP_REFERER': 'referrer',
        'HTTP_USER_AGENT': 'ua',
        'REQUEST_ID': 'rid',
    }

    Context.track('sql', 1.0)
    Context.track('http', 2.0)
    Context.track('logging', 3.0)
    run_wsgi(env, headers=[('X-View-Name', 'view')])

    # check for explicit order preservation
    log = context.logs.find(msg='GET /foo?')
    assert log is not None
    assert list(log.extra.items()) == [
        ('method', 'GET'),
        ('path', '/foo'),
        ('qs', 'bar=baz'),
        ('status', 200),
        ('view', 'view'),
        ('duration_ms', 1000.0),
        ('ip', '127.0.0.1'),
        ('proto', 'HTTP/1.0'),
        ('length', 1000),
        ('request_length', 100),
        ('request_type', 'application/json'),
        ('referrer', 'referrer'),
        ('forwarded', '203.0.113.195, 150.172.238.178'),
        ('ua', 'ua'),
        ('http_count', 1),
        ('http_time_ms', 2.0),
        ('logging_count', 1),
        ('logging_time_ms', 3.0),
        ('sql_count', 1),
        ('sql_time_ms', 1.0),
    ]

    assert context.statsd[0] == 'wsgi.requests.view.GET.200:1|c'
    assert context.statsd[1] == 'wsgi.latency.view.GET.200:1000.000000|ms'


def test_wsgi_request_log_error(run_wsgi, context):
    run_wsgi(status='500 Internal Error', headers=[('X-View-Name', 'view')])
    context.assert_log(
        name='talisker.wsgi',
        msg='GET /',
        extra=dict([
            ('method', 'GET'),
            ('path', '/'),
            ('status', 500),
            ('duration_ms', 1000.0),
            ('ip', '127.0.0.1'),
            ('proto', 'HTTP/1.0'),
            ('length', 1000),
        ]),
    )

    assert context.statsd[0] == 'wsgi.requests.view.GET.500:1|c'
    assert context.statsd[1] == 'wsgi.latency.view.GET.500:1000.000000|ms'
    assert context.statsd[2] == 'wsgi.errors.view.GET.500:1|c'


def test_wsgi_request_log_timeout(wsgi_env, context):
    wsgi_env['VIEW_NAME'] = 'view'
    request = wsgi.TaliskerWSGIRequest(wsgi_env, start_response, [])
    request.log(1, timeout=True)
    context.assert_log(
        name='talisker.wsgi',
        msg='GET /',
        extra=dict([
            ('method', 'GET'),
            ('path', '/'),
            ('duration_ms', 1000.0),
            ('ip', '127.0.0.1'),
            ('proto', 'HTTP/1.0'),
            ('timeout', True),
        ]),
    )

    assert context.statsd[0] == 'wsgi.requests.view.GET.timeout:1|c'
    assert context.statsd[1] == 'wsgi.latency.view.GET.timeout:1000.000000|ms'
    assert context.statsd[2] == 'wsgi.timeouts.view.GET:1|c'


def test_wsgi_request_log_raises(run_wsgi, context, monkeypatch):

    def error(*args, **kwargs):
        raise Exception('error')

    monkeypatch.setattr(wsgi.TaliskerWSGIRequest, 'get_metadata', error)

    run_wsgi(status='500 Internal Error')
    context.assert_log(
        name='talisker.wsgi',
        level='error',
        msg='error generating access log',
    )

    assert context.statsd == []


def test_middleware_basic(wsgi_env, start_response, context):

    def app(environ, _start_response):
        _start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'OK']

    extra_env = {'ENV': 'VALUE'}
    extra_headers = {'Some-Header': 'value'}
    wsgi_env['HTTP_X_REQUEST_ID'] = 'ID'

    mw = wsgi.TaliskerMiddleware(app, extra_env, extra_headers)
    output = b''.join(mw(wsgi_env, start_response))

    assert output == b'OK'
    assert wsgi_env['ENV'] == 'VALUE'
    assert wsgi_env['REQUEST_ID'] == 'ID'
    assert start_response.status == '200 OK'
    assert start_response.headers == [
        ('Content-Type', 'text/plain'),
        ('Some-Header', 'value'),
        ('X-Request-Id', 'ID'),
    ]

    context.assert_log(
        name='talisker.wsgi',
        msg='GET /',
        extra={'request_id': 'ID'},
    )


def test_middleware_sets_deadlines(wsgi_env, start_response, config):
    config['TALISKER_SOFT_REQUEST_TIMEOUT'] = 1000
    config['TALISKER_REQUEST_TIMEOUT'] = 2000

    contexts = []

    def app(environ, _start_response):
        contexts.append(Context.current)
        _start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'OK']

    mw = wsgi.TaliskerMiddleware(app, {}, {})
    list(mw(wsgi_env, start_response))

    assert contexts[0].soft_timeout == 1000
    assert contexts[0].deadline == contexts[0].start_time + 2.0


def test_middleware_sets_header_deadline(wsgi_env, start_response, config):
    config['TALISKER_REQUEST_TIMEOUT'] = 2000

    contexts = []

    def app(environ, _start_response):
        contexts.append(Context.current)
        _start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'OK']

    ts = datetime.utcnow() + timedelta(seconds=10)
    wsgi_env['HTTP_X_REQUEST_DEADLINE'] = ts.isoformat() + 'Z'
    mw = wsgi.TaliskerMiddleware(app, {}, {})
    list(mw(wsgi_env, start_response))

    assert contexts[0].deadline == datetime_to_timestamp(ts)


def test_middleware_error_before_start_response(
        wsgi_env, start_response, context):

    def app(environ, _start_response):
        raise Exception('error')

    extra_env = {'ENV': 'VALUE'}
    extra_headers = {'Some-Header': 'value'}
    wsgi_env['HTTP_X_REQUEST_ID'] = 'ID'
    wsgi_env['HTTP_ACCEPT'] = 'application/json'

    mw = wsgi.TaliskerMiddleware(app, extra_env, extra_headers)
    output = b''.join(mw(wsgi_env, start_response))
    error = json.loads(output.decode('utf8'))

    assert error['title'] == 'Request ID: Exception'
    assert wsgi_env['ENV'] == 'VALUE'
    assert wsgi_env['REQUEST_ID'] == 'ID'
    assert start_response.status == '500 Internal Server Error'
    assert start_response.exc_info[0] is Exception
    assert start_response.headers[:3] == [
        ('Content-Type', 'application/json'),
        ('Some-Header', 'value'),
        ('X-Request-Id', 'ID'),
    ]
    if talisker.sentry.enabled:
        assert start_response.headers[3] == (
            'X-Sentry-ID', wsgi_env['SENTRY_ID']
        )

    context.assert_log(
        name='talisker.wsgi',
        msg='GET /',
        extra={
            'status': 500,
            'exc_type': 'Exception',
        },
    )


def test_middleware_error_after_start_response(
        wsgi_env, start_response, context):

    def app(wsgi_env, _start_response):
        _start_response('200 OK', [('Content-Type', 'application/json')])
        raise Exception('error')

    extra_env = {'ENV': 'VALUE'}
    extra_headers = {'Some-Header': 'value'}
    wsgi_env['HTTP_X_REQUEST_ID'] = 'ID'
    wsgi_env['HTTP_ACCEPT'] = 'application/json'

    mw = wsgi.TaliskerMiddleware(app, extra_env, extra_headers)
    output = b''.join(mw(wsgi_env, start_response))
    error = json.loads(output.decode('utf8'))

    assert error['title'] == 'Request ID: Exception'
    assert wsgi_env['ENV'] == 'VALUE'
    assert wsgi_env['REQUEST_ID'] == 'ID'
    assert start_response.status == '500 Internal Server Error'
    assert start_response.headers[:3] == [
        ('Content-Type', 'application/json'),
        ('Some-Header', 'value'),
        ('X-Request-Id', 'ID'),
    ]
    if talisker.sentry.enabled:
        assert start_response.headers[3] == (
            'X-Sentry-ID', wsgi_env['SENTRY_ID']
        )

    context.assert_log(
        name='talisker.wsgi',
        msg='GET /',
        extra={
            'status': 500,
            'exc_type': 'Exception',
        },
    )


def test_middleware_preserves_file_wrapper(
        wsgi_env, start_response, context, tmpdir):
    path = tmpdir.join('filecontent')
    path.write('CONTENT')

    def app(environ, _start_response):
        _start_response('200 OK', [('Content-Type', 'text/plain')])
        return environ['wsgi.file_wrapper'](open(str(path)))

    mw = wsgi.TaliskerMiddleware(app, {}, {})
    wsgi_env['wsgi.file_wrapper'] = wsgiref.util.FileWrapper

    with freeze_time() as frozen:
        respiter = mw(wsgi_env, start_response)
        context.assert_not_log(msg='GET /')
        frozen.tick(1.0)
        respiter.close()

    assert isinstance(respiter, wsgiref.util.FileWrapper)
    context.assert_log(
        msg='GET /',
        extra=dict([
            ('method', 'GET'),
            ('path', '/'),
            ('status', 200),
            ('duration_ms', 1000.0),
            ('ip', '127.0.0.1'),
            ('proto', 'HTTP/1.0'),
            ('length', len('CONTENT')),
            ('filepath', str(path)),
        ]),
    )


def test_middleware_debug_middleware_error(wsgi_env, start_response, context):
    from werkzeug.debug import DebuggedApplication

    def app(environ, _):
        raise Exception('error')

    mw = wsgi.TaliskerMiddleware(DebuggedApplication(app), {}, {})

    wsgi_env['HTTP_X_REQUEST_ID'] = 'ID'
    list(mw(wsgi_env, start_response))

    assert start_response.status == '500 INTERNAL SERVER ERROR'
    assert start_response.headers == [
        ('Content-Type', 'text/html; charset=utf-8'),
        ('X-XSS-Protection', '0'),
        ('X-Request-Id', 'ID'),
    ]

    context.assert_log(name='talisker.wsgi', msg='GET /')


def test_middleware_debug_middleware(wsgi_env, start_response, context):
    from werkzeug.debug import DebuggedApplication

    # DebuggedApplication turns any WSGI app into a super lazy version
    def app(environ, start_response):
        start_response('302 Found', [('Location', '/other')])
        yield b''

    mw = wsgi.TaliskerMiddleware(DebuggedApplication(app), {}, {})

    wsgi_env['HTTP_X_REQUEST_ID'] = 'ID'
    output = b''.join(mw(wsgi_env, start_response))

    assert start_response.status == '302 Found'
    assert output == b''
    assert start_response.headers == [
        ('Location', '/other'),
        ('X-Request-Id', 'ID'),
    ]


def test_middleware_debug_middleware_no_content(
        wsgi_env, start_response, context):
    from werkzeug.debug import DebuggedApplication

    # DebuggedApplication turns any WSGI app into a super lazy version
    def app(environ, start_response):
        start_response('304 Not Modified', [])
        # no content
        return []

    mw = wsgi.TaliskerMiddleware(DebuggedApplication(app), {}, {})

    output = b''.join(mw(wsgi_env, start_response))

    assert start_response.status == '304 Not Modified'
    assert output == b''


def test_wrap():

    def app(environ, start_response):
        start_response(200, [])
        return environ

    wrapped = wsgi.wrap(app)

    assert wrapped._talisker_wrapped is True
    assert wrapped._talisker_original_app is app
    assert wrapped is not app

    wrapped2 = wsgi.wrap(wrapped)
    assert wrapped2 is wrapped
