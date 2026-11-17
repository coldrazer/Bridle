from test_api import client_for, CTL


def test_studio_routes_share_control_auth_and_project_persistence(tmp_path):
    client, _ = client_for(tmp_path / 'models', tmp_path)
    with client:
        for path in ['/control/operations', '/control/intelligence/projects']:
            assert client.get(path).status_code == 401
            assert client.get(path, headers=CTL).status_code == 200
        project = client.post('/control/intelligence/projects', headers=CTL,
                              json={'name': 'My research'}).json()
        result = client.post('/control/intelligence/projects/' + project['id'] + '/documents',
                             headers=CTL, json={'name': 'notes.md', 'text': 'Bridle runs locally on your Mac.'})
        assert result.status_code == 200
        citations = client.post('/control/intelligence/projects/' + project['id'] + '/search',
                                headers=CTL, json={'query': 'Bridle'}).json()['citations']
        assert citations[0]['filename'] == 'notes.md'
        assert client.put('/control/operations/profile', headers=CTL,
                          json={'name': 'quiet'}).status_code == 200
        assert client.get('/control/operations', headers=CTL).json()['profile'] == 'quiet'


def test_revoking_last_device_does_not_reopen_remote_access(tmp_path):
    client, engine = client_for(tmp_path / 'models', tmp_path, local=False, host='0.0.0.0')
    with client:
        key, secret = engine.keys.create('Phone')
        assert client.get('https://testserver/v1/models', headers={'authorization': 'Bearer ' + secret}).status_code == 200
        engine.keys.revoke(key.id)
        assert client.get('https://testserver/v1/models', headers={'authorization': 'Bearer ' + secret}).status_code == 401
        assert client.get('https://testserver/v1/models').status_code == 401
