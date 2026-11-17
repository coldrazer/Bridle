import pytest
from bridle_engine.catalog import ModelStore
from bridle_engine.domain import RepoId
from bridle_engine.imports import scan_models, register_import


def fixture_model(path):
    path.mkdir(parents=True)
    (path/'config.json').write_text('{"model_type":"qwen2"}')
    (path/'model.safetensors').write_bytes(b'weights')
    (path/'tokenizer.json').write_text('{}')
    return path


def test_scan_finds_nested_snapshot_and_unsupported(tmp_path):
    fixture_model(tmp_path/'models--owner--name'/'snapshots'/'revision')
    (tmp_path/'other.gguf').write_bytes(b'GGUF')
    result = scan_models(tmp_path)
    assert len(result['models']) == 1
    assert result['unsupported'][0]['format'] == 'GGUF'


def test_import_is_durable_and_remove_keeps_original(tmp_path):
    source = fixture_model(tmp_path/'external'/'my-model')
    store = ModelStore(tmp_path/'managed')
    model = register_import(store, source)
    repo = RepoId.parse(model['repo'])
    assert ModelStore(store.root).get(repo).path == str(source)
    assert str(repo) in [m.repo for m in store.list()]
    assert store.delete(repo)
    assert (source/'model.safetensors').exists()


def test_invalid_and_duplicate_import(tmp_path):
    source = fixture_model(tmp_path/'external')
    store = ModelStore(tmp_path/'managed')
    first = register_import(store, source)
    assert register_import(store, source)['repo'] == first['repo']
    (source/'config.json').write_text('[]')
    with pytest.raises(ValueError): register_import(store, source)


def test_import_routes_require_control_auth_and_expose_to_catalog(tmp_path):
    from test_api import client_for, CTL
    source = fixture_model(tmp_path/'external')
    c, engine = client_for(tmp_path/'models', tmp_path/'data')
    with c:
        assert c.post('/control/imports/scan', json={'path': str(source)}).status_code == 401
        scan = c.post('/control/imports/scan', headers=CTL, json={'path': str(source)})
        assert len(scan.json()['models']) == 1
        imported = c.post('/control/imports/register', headers=CTL, json={'path': str(source)}).json()
        assert str(engine.store.get(RepoId.parse(imported['repo'])).path) == str(source)
        catalog = c.get('/control/catalog', headers=CTL).json()['models']
        assert any(m['repo'] == imported['repo'] and m['downloaded'] for m in catalog)
        assert not (source/'.bridle.json').exists()
