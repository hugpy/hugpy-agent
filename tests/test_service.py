"""Session HTTP contract and real agent-loop integration, with no external services."""
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from hugpy_agent.config import Config
from hugpy_agent.service.profiles import load_profiles
from hugpy_agent.service.runtime import Runtime
from hugpy_agent.service.providers import ProviderGateway
from helpers import FakeGateway, tc


def runtime(tmp_path):
    profiles = tmp_path / 'profiles.json'
    profiles.write_text(json.dumps({'discover_clients': False, 'profiles': {
        'local': {'base_url': 'http://localhost:8000/v1', 'model': 'test', 'context_length': 32768}}}))
    return Runtime(Config(workspace=str(tmp_path), audit_log='', max_steps=5, rag_enabled=False), tmp_path / 'state', profiles)


def wait(rt, sid, statuses):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        view = rt.view(sid)
        if view['status'] in statuses:
            return view
        time.sleep(.01)
    pytest.fail(str(rt.view(sid)))


def test_real_loop_and_continuation(tmp_path):
    rt = runtime(tmp_path)
    gw = FakeGateway([tc('fs_glob', pattern='*'), tc('final_answer', answer='first'), tc('fs_glob', pattern='*'), tc('final_answer', answer='second')])
    with patch('hugpy_agent.service.runtime.gateway', return_value=gw), patch('hugpy_agent.tools.toolserver.specs', return_value=[]):
        sid = rt.create()['id']
        rt.start(sid, 'Say first')
        one = wait(rt, sid, {'done', 'aborted'})
        assert one['status'] == 'done', one
        assert one['run_id']
        rt.start(sid, 'Say second')
        two = wait(rt, sid, {'done', 'aborted'})
        assert two['report']['answer'] == 'second', two
        assert [e['data'] for e in two['events'] if e['kind'] == 'reply'] == ['first', 'second']
    assert rt.store.session(sid)['status'] == 'done'


def test_approval_and_stop(tmp_path):
    rt = runtime(tmp_path)
    gw = FakeGateway([tc('shell', command='echo approved'), tc('final_answer', answer='done')])
    with patch('hugpy_agent.service.runtime.gateway', return_value=gw), patch('hugpy_agent.tools.toolserver.specs', return_value=[]):
        sid = rt.create()['id']
        rt.start(sid, 'Run echo')
        view = wait(rt, sid, {'waiting', 'aborted', 'done'})
        assert view['status'] == 'waiting', view
        with pytest.raises(ValueError):
            rt.answer(sid, view['pending']['id'], 'random')
        rt.stop(sid)
        assert wait(rt, sid, {'interrupted'})['status'] == 'interrupted'


def test_discovery_never_executes(tmp_path):
    p = tmp_path / 'profiles.json'
    p.write_text('{"profiles": {}}')
    with patch('hugpy_agent.service.profiles.binary', side_effect=lambda name: '/fake/codex' if name == 'codex' else None), patch('subprocess.Popen', side_effect=AssertionError('must not launch')):
        default, profiles = load_profiles(p)
    assert default == 'codex'
    assert list(profiles) == ['codex']


@pytest.mark.parametrize('protocol,response,route', [
    ('anthropic', {'content': [{'type': 'text', 'text': 'answer'}]}, '/messages'),
    ('openai-responses', {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'answer'}]}]}, '/responses'),
])
def test_provider_wire(protocol, response, route):
    import io
    profile = dict(protocol=protocol, base_url='http://localhost/v1', model='test', context_length=8192)
    seen = []
    def open_request(request, **kwargs):
        seen.append(request)
        return io.BytesIO(json.dumps(response).encode())
    with patch('urllib.request.urlopen', side_effect=open_request):
        result = ProviderGateway(profile).chat([{'role': 'user', 'content': 'hi'}])
    assert result.ok and result.text == 'answer'
    assert seen[0].full_url.endswith(route)
    assert json.loads(seen[0].data)['model'] == 'test'


def test_http_auth_sessions_and_restart(tmp_path):
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from hugpy_agent.service.http import handler
    rt = runtime(tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(rt, 'test-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%s' % server.server_port
    def request(path, body=None, token='test-token'):
        req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urlopen(req) as response:
            return json.load(response)
    try:
        with pytest.raises(HTTPError) as error:
            request('/api/state', token='wrong')
        assert error.value.code == 401
        assert request('/api/state')['service'] == 'hugpy-agent'
        sid = request('/api/sessions', {})['id']
        assert request('/api/sessions/' + sid)['status'] == 'idle'
        rt.store.query("UPDATE sessions SET status='running' WHERE id=?", (sid,))
        assert Runtime(rt.cfg, rt.root, rt.profiles_path).view(sid)['status'] == 'interrupted'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_switch_model_keeps_session_and_transcript(tmp_path):
    rt = runtime(tmp_path)
    doc = json.loads(Path(rt.profiles_path).read_text())
    doc['profiles']['other'] = dict(doc['profiles']['local'], model='other-model')
    Path(rt.profiles_path).write_text(json.dumps(doc))
    sid = rt.create()['id']
    rt.store.event(sid, 'user', 'remember this')
    rt.store.event(sid, 'reply', 'remembered')
    rt.store.query("UPDATE sessions SET native_id='old-client',run_id='old-run' WHERE id=?", (sid,))
    switched = rt.select_profile(sid, 'other')
    assert switched['id'] == sid and switched['profile'] == 'other'
    assert switched['native_id'] is None and switched['run_id'] is None
    assert switched['events'][0]['data'] == 'remember this'
    rt.active[sid] = {}
    with pytest.raises(RuntimeError):
        rt.select_profile(sid, 'local')


def test_fleet_catalog_filters_nonchat_models(tmp_path):
    rt = runtime(tmp_path)
    doc=json.loads(Path(rt.profiles_path).read_text())
    doc['profiles']['local'].update(protocol='hugpy',discover_models=True)
    Path(rt.profiles_path).write_text(json.dumps(doc))
    rt.catalog_at=0
    with patch('hugpy_agent.gateway.Gateway.models',return_value=[
        {'id':'coder','tasks':['text-generation'],'context_length':8192},
        {'id':'whisper','tasks':['automatic-speech-recognition']},
        {'id':'blocked','blocked':True}]):
        rt.profiles()
        rt.catalog_thread.join(timeout=2)
        _,profiles=rt.profiles()
    assert 'local:coder' in profiles
    assert 'local:whisper' not in profiles and 'local:blocked' not in profiles


def test_fleet_uses_normal_eviction_policy():
    from hugpy_agent.service.providers import FleetGateway
    cfg=Config()
    assert FleetGateway({},cfg).build_payload([])['no_makeroom'] is False
    assert FleetGateway({'allow_eviction':False},cfg).build_payload([])['no_makeroom'] is True


def test_conversation_can_answer_without_unnecessary_tools(tmp_path):
    rt = runtime(tmp_path)
    for reply in ['violet-orchid-729', tc('final_answer', answer='violet-orchid-729')]:
        gw = FakeGateway([reply])
        with patch('hugpy_agent.service.runtime.gateway', return_value=gw), patch('hugpy_agent.tools.toolserver.specs', return_value=[]):
            sid = rt.create()['id']
            rt.start(sid, 'Reply with the marker. No tools needed.')
            view = wait(rt, sid, {'done', 'aborted', 'waiting'})
            assert view['status'] == 'done', view
            assert view['report']['answer'] == 'violet-orchid-729'
            assert not view.get('pending')
