"""Persistent intelligence behavior, using a fake only at the inference boundary."""
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridle_engine.intelligence import IntelligenceStore, register_intelligence_routes


def wait(store, job_id):
    for _ in range(200):
        job = store.get('jobs', job_id)
        if job['status'] not in ('pending', 'running'):
            return job
        time.sleep(.01)
    pytest.fail('job did not finish')


def test_workspace_persists_and_retrieval_is_project_isolated(tmp_path):
    s = IntelligenceStore(tmp_path, lambda p, m, n, c, system: 'answer')
    a, b = s.create_project('A'), s.create_project('B')
    s.add_document(a['id'], {'name': 'Guide.pdf', 'pages': ['Apples are red.', 'Oranges contain vitamin C.']})
    found = s.search(a['id'], 'vitamin')
    assert found['method'] == 'lexical'
    assert found['citations'][0]['page'] == 2
    assert found['citations'][0]['filename'] == 'Guide.pdf'
    assert s.search(b['id'], 'vitamin')['citations'] == []
    s.append_chat(a['id'], {'role': 'user', 'content': 'hello'})
    s.close()
    reopened = IntelligenceStore(tmp_path, lambda *a: '')
    assert reopened.list('projects')[0]['name'] in ('A', 'B')
    assert reopened.list('chat', a['id'])[0]['content'] == 'hello'
    with pytest.raises(ValueError):
        reopened.search('../escape', 'x')
    reopened.close()


def test_prompt_versions_and_real_job_result_and_evaluation(tmp_path):
    seen = []
    def generate(prompt, model, tokens, cancel, system):
        seen.append(prompt)
        return 'Actual blue output'
    s = IntelligenceStore(tmp_path, generate)
    p = s.create_project('A')['id']
    one = s.save_prompt({'project_id': p, 'name': 'Color', 'template': 'First {input}'})
    two = s.save_prompt({'project_id': p, 'id': one['id'], 'name': 'Color', 'template': 'Next {input}'})
    assert [v['version'] for v in two['versions']] == [1, 2]
    job = s.submit({'project_id': p, 'prompt': 'Say blue'})
    assert wait(s, job['id'])['result']['text'] == 'Actual blue output'
    suite = s.save_evaluation({'project_id': p, 'name': 'Colors', 'cases': [{'prompt': 'Color?', 'expected': 'blue'}]})
    result = wait(s, s.run_evaluation(suite['id'])['id'])['result']
    assert result['passed'] == 1 and result['cases'][0]['actual'] == 'Actual blue output'
    assert seen == ['Say blue', 'Color?']
    s.close()


def test_typed_workflow_retrieves_generates_validates_exports(tmp_path):
    s = IntelligenceStore(tmp_path, lambda prompt, *a: 'red apples' if 'orchard.md' in prompt else 'bad')
    p = s.create_project('A')['id']
    s.add_document(p, {'name': 'orchard.md', 'text': 'apples grow red'})
    wf = s.save_workflow({'project_id': p, 'name': 'Answer', 'steps': [
        {'type': 'input'}, {'type': 'retrieve'}, {'type': 'prompt', 'template': '{input}\n{context}'},
        {'type': 'validate', 'contains': 'apples'}, {'type': 'export'}]})
    result = wait(s, s.run_workflow(wf['id'], {'input': 'apples'})['id'])
    assert result['status'] == 'completed'
    assert result['result']['citations'][0]['filename'] == 'orchard.md'
    assert (tmp_path / result['result']['export']).read_text() == 'red apples'
    with pytest.raises(ValueError):
        s.save_workflow({'project_id': p, 'name': 'unsafe', 'steps': [{'type': 'shell', 'command': 'id'}]})
    with pytest.raises(ValueError):
        s.save_workflow({'project_id': p, 'name': 'unsafe', 'steps': [{'type': 'export', 'path': '/tmp/x'}]})
    s.close()


def test_cancellation_failure_and_restart_do_not_fake_success(tmp_path):
    started = threading.Event()
    def generate(prompt, model, tokens, cancel, system):
        started.set()
        cancel.wait(2)
        return 'partial'
    s = IntelligenceStore(tmp_path, generate)
    p = s.create_project('A')['id']
    job = s.submit({'project_id': p, 'prompt': 'test'})
    assert started.wait(1)
    s.cancel(job['id'])
    assert wait(s, job['id'])['status'] == 'cancelled'
    s.close()
    s = IntelligenceStore(tmp_path, lambda *a: (_ for _ in ()).throw(RuntimeError('Load a model first')))
    failed = wait(s, s.submit({'project_id': p, 'prompt': 'test'})['id'])
    assert failed['status'] == 'failed' and 'Load a model' in failed['error']
    # Durable unfinished work is explicitly interrupted, never silently rerun.
    failed['status'] = 'pending'
    s.put('jobs', failed)
    s.close()
    s = IntelligenceStore(tmp_path, lambda *a: 'retry')
    assert s.get('jobs', failed['id'])['status'] == 'interrupted'
    assert wait(s, s.retry(failed['id'])['id'])['result']['text'] == 'retry'
    s.close()


def test_ask_carries_citations_and_refuses_empty_retrieval(tmp_path):
    seen = []
    def generate(prompt, *args):
        seen.append(prompt)
        return 'The orchard grows apples [1].'
    s = IntelligenceStore(tmp_path, generate)
    p = s.create_project('A')['id']
    s.add_document(p, {'name': 'orchard.txt', 'text': 'The orchard grows apples.'})
    job = wait(s, s.ask(p, {'query': 'apples'})['id'])
    assert job['status'] == 'completed'
    assert job['result']['citations'][0]['filename'] == 'orchard.txt'
    assert '[1] orchard.txt page 1 chunk 1' in seen[0]
    with pytest.raises(ValueError, match='No matching'):
        s.ask(p, {'query': 'zebras'})
    s.close()


def test_routes_validate_json_and_runtime_errors_are_failures(tmp_path):
    from types import SimpleNamespace
    class Runtime:
        def resolve(self, model, auto_load):
            return object()
        def chat_prompt(self, lm, messages):
            return messages[0].content
        def submit_generation(self, job):
            job.emit(RuntimeError('device failure'))
            job.emit(None)
    app = FastAPI()
    s = register_intelligence_routes(app, SimpleNamespace(home=tmp_path, runtime=Runtime()))
    c = TestClient(app)
    prefix = '/control/intelligence'
    assert c.post(prefix + '/projects', content='[]').status_code == 400
    p = c.post(prefix + '/projects', json={'name': 'A'}).json()['id']
    assert c.post(prefix + '/projects/' + p + '/documents', json={'name': '../x.txt', 'text': 'x'}).status_code == 400
    job = c.post(prefix + '/jobs', json={'project_id': p, 'prompt': 'test'}).json()
    failed = wait(s, job['id'])
    assert failed['status'] == 'failed'
    assert 'device failure' in failed['error']
    s.close()


def test_project_settings_survive_restart_and_validate_types(tmp_path):
    s = IntelligenceStore(tmp_path, lambda *a: '')
    p = s.create_project('Research')
    updated = s.update_project(p['id'], {'name': 'Research', 'model': 'org/model', 'system_prompt': 'Be concise.'})
    assert updated['model'] == 'org/model' and updated['system_prompt'] == 'Be concise.'
    with pytest.raises(ValueError):
        s.update_project(p['id'], {'system_prompt': ['invalid']})
    s.close()
    s = IntelligenceStore(tmp_path, lambda *a: '')
    assert s.project(p['id'])['system_prompt'] == 'Be concise.'
    s.close()


def test_chunked_request_rejected_without_reading_remaining_body(tmp_path):
    import asyncio
    import httpx
    from types import SimpleNamespace
    app = FastAPI()
    s = register_intelligence_routes(app, SimpleNamespace(home=tmp_path, runtime=None))
    consumed = []
    async def chunks():
        for index in range(4):
            consumed.append(index)
            yield b'x' * 2_100_000
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/control/intelligence/projects', content=chunks())
            assert response.status_code == 413
    asyncio.run(send())
    assert consumed == [0, 1]
    s.close()


def test_jobs_snapshot_project_defaults_and_allow_explicit_overrides(tmp_path):
    seen = []
    def generate(prompt, model, tokens, cancel, system):
        seen.append((prompt, model, system))
        return 'done'
    s = IntelligenceStore(tmp_path, generate)
    p = s.create_project('A')['id']
    s.update_project(p, {'model': 'org/saved', 'system_prompt': 'Be exact.'})
    job = wait(s, s.submit({'project_id': p, 'prompt': 'first'})['id'])
    assert job['model'] == 'org/saved' and job['system_prompt'] == 'Be exact.'
    wait(s, s.submit({'project_id': p, 'prompt': 'second', 'model': 'org/override', 'system_prompt': ''})['id'])
    assert seen == [('first', 'org/saved', 'Be exact.'), ('second', 'org/override', '')]
    s.close()


@pytest.mark.parametrize('actual,schema,status', [
    ('{"count": 2}', {'type': 'object', 'required': ['count'], 'properties': {'count': {'type': 'integer'}}}, 'completed'),
    ('not JSON', None, 'failed'),
    ('{"count": "two"}', {'type': 'object', 'properties': {'count': {'type': 'integer'}}}, 'failed'),
])
def test_workflow_json_validation_checks_actual_output(tmp_path, actual, schema, status):
    s = IntelligenceStore(tmp_path, lambda *a: actual)
    p = s.create_project('A')['id']
    validation = {'type': 'validate', 'format': 'json'}
    if schema is not None:
        validation['schema'] = schema
    workflow = s.save_workflow({'project_id': p, 'name': 'JSON', 'steps': [
        {'type': 'prompt', 'template': 'Return JSON for {input}'}, validation]})
    job = wait(s, s.run_workflow(workflow['id'], {'input': 'count'})['id'])
    assert job['status'] == status
    if status == 'completed':
        assert job['result']['validated'] is True
    else:
        assert 'JSON validation failed' in job['error']
    s.close()


@pytest.mark.parametrize('validation', [
    {'type': 'validate', 'format': 'xml'},
    {'type': 'validate', 'format': 'json', 'schema': []},
    {'type': 'validate', 'format': 'json', 'schema': {'type': 'bogus'}},
    {'type': 'validate', 'format': 'json', 'schema': {'$ref': 'https://example.com/schema'}},
    {'type': 'validate', 'schema': {'type': 'object'}, 'contains': 'x'},
])
def test_workflow_rejects_invalid_json_validation_configuration(tmp_path, validation):
    s = IntelligenceStore(tmp_path, lambda *a: '{}')
    p = s.create_project('A')['id']
    with pytest.raises(ValueError):
        s.save_workflow({'project_id': p, 'name': 'JSON', 'steps': [
            {'type': 'prompt', 'template': '{input}'}, validation]})
    s.close()
