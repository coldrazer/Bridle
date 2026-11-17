"""Read-only local model discovery and durable references to user-owned folders."""
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from fastapi import HTTPException, Request

_LOCK = threading.RLock()


def registry(store):
    path = store.root / '.imports.json'
    if not path.exists(): return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict): raise ValueError('Imported model registry is invalid')
    except (OSError, ValueError) as exc:
        # Match KeyStore/SettingsStore: quarantine the corrupt file and start
        # empty instead of failing every model listing.
        import logging as _logging, time as _time
        backup = path.with_name(path.name + f'.corrupt-{int(_time.time())}')
        try:
            path.rename(backup)
            _logging.getLogger('bridle.imports').error(
                'imported-model registry %s is corrupt (%s); preserved as %s and starting empty', path, exc, backup)
        except OSError:
            _logging.getLogger('bridle.imports').error(
                'imported-model registry %s is corrupt (%s); starting empty', path, exc)
        return {}
    return value


def save_registry(store, value):
    import tempfile as _tempfile
    store.root.mkdir(parents=True, exist_ok=True)
    path = store.root / '.imports.json'
    fd, name = _tempfile.mkstemp(prefix='.imports-', dir=path.parent)  # 0600 from creation
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(value, indent=2))
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_model(path):
    path = Path(path).expanduser().resolve()
    config = path / 'config.json'
    if not config.is_file() or config.stat().st_size > 1_000_000:
        raise ValueError('Missing or oversized config.json')
    cfg = json.loads(config.read_text())
    if not isinstance(cfg, dict) or not isinstance(cfg.get('model_type'), str):
        raise ValueError('config.json must describe a model_type')
    weights = list(path.glob('*.safetensors'))
    if not weights or any(not f.is_file() or f.stat().st_size == 0 for f in weights):
        raise ValueError('No complete Safetensors weights found')
    index = path / 'model.safetensors.index.json'
    if index.exists():
        data = json.loads(index.read_text())
        names = set(data.get('weight_map', {}).values())
        if not names or any(not isinstance(n, str) or Path(n).name != n or not (path/n).is_file() for n in names):
            raise ValueError('Weight shards are missing or the index is invalid')
    if not any((path/name).is_file() for name in ('tokenizer.json', 'tokenizer.model', 'vocab.json')):
        raise ValueError('Tokenizer files are missing')
    name = path.parent.parent.name.removeprefix('models--').replace('--', '/') if path.parent.name == 'snapshots' else path.name
    return {'path': str(path), 'name': name, 'model_type': cfg['model_type'],
            'size_bytes': sum(f.stat().st_size for f in weights), 'status': 'candidate',
            'detail': 'Safetensors candidate. Actual architecture compatibility is checked when loaded.'}


def scan_models(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir(): raise ValueError('Choose an existing folder')
    found, unsupported, warnings = [], [], []
    visited = 0
    def onerror(error): warnings.append(str(error))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        visited += 1
        relative = Path(directory).relative_to(root)
        dirs[:] = [d for d in dirs if d not in {'.git', '.venv', '.build', '__pycache__', 'blobs'}]
        if len(relative.parts) >= 8: dirs[:] = []
        if visited > 5000 or len(found) >= 200:
            warnings.append('Scan limit reached. Choose a smaller folder to see more models.'); break
        if 'config.json' in files:
            try: found.append(inspect_model(Path(directory)))
            except (ValueError, OSError, TypeError, AttributeError) as exc:
                unsupported.append({'path': directory, 'format': 'Safetensors/config', 'detail': str(exc)})
        for filename in files:
            if filename.lower().endswith('.gguf') and len(unsupported) < 300:
                unsupported.append({'path': str(Path(directory)/filename), 'format': 'GGUF',
                    'detail': 'This MLX engine cannot host GGUF. Use an MLX/Safetensors version; files are unchanged.'})
    return {'root': str(root), 'models': found, 'unsupported': unsupported[:300], 'warnings': warnings[:20]}


def register_import(store, path):
    candidate = inspect_model(path)
    with _LOCK:
        items = registry(store)
        for repo, meta in items.items():
            if meta['path'] == candidate['path']: return {'repo': repo, **meta}
        slug = re.sub('[^A-Za-z0-9_-]', '-', candidate['name'])[:45].strip('-') or 'model'
        repo = 'local/' + slug + '-' + hashlib.sha256(candidate['path'].encode()).hexdigest()[:12]
        items[repo] = {**candidate, 'revision': 'local', 'imported': True}
        save_registry(store, items)
    return {'repo': repo, **items[repo]}


def register_import_routes(app, engine):
    async def body(request):
        from .api import body_json
        value = await body_json(request)
        if not isinstance(value, dict) or not isinstance(value.get('path'), str) or len(value['path']) > 4096:
            raise HTTPException(400, 'Choose a local folder')
        return value
    @app.post('/control/imports/scan')
    async def scan(request: Request):
        import asyncio
        try: return await asyncio.to_thread(scan_models, (await body(request))['path'])
        except (ValueError, OSError) as exc: raise HTTPException(400, str(exc)) from exc
    @app.post('/control/imports/register')
    async def register(request: Request):
        import asyncio
        try: return await asyncio.to_thread(register_import, engine.store, (await body(request))['path'])
        except (ValueError, OSError, TypeError, AttributeError) as exc: raise HTTPException(400, str(exc)) from exc
