import sys
import time
import uuid
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import app as application
import engine
import store


@pytest.fixture
def client(monkeypatch):
    tmp_path = Path(__file__).resolve().parents[1]/'artifacts'/'unit'/uuid.uuid4().hex
    tmp_path.mkdir(parents=True)
    monkeypatch.setattr(store,'DATA',tmp_path)
    monkeypatch.setattr(engine,'DATA',tmp_path)
    application.setup()
    application.app.config['TESTING']=True
    import authentication
    authentication.config()
    test_client=application.app.test_client()
    login=test_client.post('/api/auth/login',json={'password':(tmp_path/'admin-password.txt').read_text().strip()})
    assert login.status_code==200
    test_client.environ_base['HTTP_X_CSRF_TOKEN']=login.json['csrf_token']
    yield test_client
    deadline=time.monotonic()+15
    while engine.ACTIVE and time.monotonic()<deadline:
        time.sleep(.02)


def wait_batch(client,bid,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rows=client.get('/api/batches').json['items']
        batch=next(r for r in rows if r['id']==bid)
        if batch['status'] in ('completed','stopped'):
            return batch
        time.sleep(.03)
    raise AssertionError('Batch failed to finish')
