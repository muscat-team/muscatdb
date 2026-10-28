"""Admin API authorization, transactional account writes and grant lifecycle."""
import sqlite3

from fastapi.testclient import TestClient
import pytest

from muscat_db import access_groups, auth, web
from muscat_db.database import _apply_schema


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = str(tmp_path / 'admin.db')
    with sqlite3.connect(path) as conn:
        _apply_schema(conn)
        conn.execute("INSERT INTO users(username,is_admin) VALUES ('admin',1),('viewer',0)")
        access_groups.ensure_seeded(conn)
        conn.commit()
    monkeypatch.setenv('MUSCAT_DB_PATH', path)
    monkeypatch.setenv('MUSCAT_PROXY_SECRET', 'test-secret')
    monkeypatch.delenv('MUSCAT_REQUIRE_AUTH', raising=False)
    return path, TestClient(web.app, client=('127.0.0.1', 1234))


def headers(user='admin'):
    return {'X-Forwarded-User': user, 'X-MuSCAT-Proxy-Secret': 'test-secret', 'Origin': 'http://testserver'}


def post(client, action, **fields):
    return client.post('/api/admin/change', headers=headers(), json={'action': action, **fields})


@pytest.mark.parametrize('user', ['', 'viewer'])
def test_non_admin_cannot_read_or_write(setup, user):
    _, client = setup
    assert client.get('/api/admin/state', headers=headers(user)).status_code == 403
    assert client.post('/api/admin/change', headers=headers(user), json={'action': 'account', 'username': 'bad', 'email': 'bad@univ.edu'}).status_code == 403
    assert 'id="admin-dashboard"' not in client.get('/settings', headers=headers(user)).text


def test_admin_card_and_csrf(setup):
    _, client = setup
    assert 'id="admin-dashboard"' in client.get('/settings', headers=headers()).text
    for origin in ['', 'https://other.example']:
        h = headers(); h['Origin'] = origin
        assert client.post('/api/admin/change', headers=h, json={'action': 'group', 'group': 'bad'}).status_code == 403
    h = headers(); h['X-MuSCAT-Proxy-Secret'] = 'wrong'
    assert client.get('/api/admin/state', headers=h).status_code == 403


def test_accounts_normalize_scope_and_clear(setup):
    path, client = setup
    assert post(client, 'account', username='new', email=' New@Univ.edu ').status_code == 200
    assert auth.username_for_email('new@univ.edu') == 'new'
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT scoped FROM users WHERE username='new'").fetchone() == (1,)
        assert conn.execute("SELECT 1 FROM group_exclusions WHERE username='new' AND groupname='everyone'").fetchone()
    assert post(client, 'scope', username='new', scoped=False).status_code == 200
    assert post(client, 'account', username='new', email='updated@univ.edu').status_code == 200
    users = client.get('/api/admin/state', headers=headers()).json()['users']
    assert not next(u for u in users if u['username'] == 'new')['scoped']
    assert post(client, 'clear_email', username='new').status_code == 200
    assert auth.username_for_email('updated@univ.edu') is None


def test_invalid_accounts_leave_no_rows(setup):
    path, client = setup
    assert post(client, 'account', username='viewer', email='v@univ.edu').status_code == 200
    for username, email in [('dup', 'V@univ.edu'), ('bad', 'not-email'), ('admin', 'admin@univ.edu'), ('x@y', 'x@univ.edu')]:
        assert post(client, 'account', username=username, email=email).status_code == 400
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM users').fetchone() == (2,)
    assert post(client, 'scope', username='admin', scoped=True).status_code == 400
    assert post(client, 'clear_email', username='missing').status_code == 400


def test_groups_grants_and_revocation_history(setup):
    path, client = setup
    assert post(client, 'group', group='team').status_code == 200
    assert post(client, 'join', group='team', username='viewer').status_code == 200
    assert post(client, 'join', group='admin', username='viewer').status_code == 400
    assert post(client, 'join', group='team', username='missing').status_code == 400
    assert post(client, 'grant', group='team', instrument='sinistro', target='TOI-123', date_from='261028').status_code == 200
    assert post(client, 'grant', group='team').status_code == 400
    assert post(client, 'grant', group='team', instrument='unknown').status_code == 400
    assert post(client, 'grant', group='team', target='TOI-123', date_from='261029', date_to='261028').status_code == 400
    with sqlite3.connect(path) as conn:
        grant_id, actor = conn.execute("SELECT id,granted_by FROM group_grants WHERE groupname='team'").fetchone()
        assert actor == 'admin'
    assert post(client, 'revoke', grant_id=grant_id).status_code == 200
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT revoked_by FROM group_grants_revoked WHERE id=?', (grant_id,)).fetchone() == ('admin',)
    assert post(client, 'leave', group='team', username='viewer').status_code == 200
    assert post(client, 'grant', group='user:viewer', proposal_id='TEST').status_code == 200
    assert post(client, 'grant', group='user:missing', proposal_id='TEST').status_code == 400


@pytest.mark.parametrize('existing, alias', [('admin', 'ADMIN'), ('viewer', 'VIEWER')])
def test_account_rejects_casefold_collision(setup, existing, alias):
    path, client = setup
    with sqlite3.connect(path) as conn:
        before = conn.execute('SELECT * FROM users ORDER BY username').fetchall()
    response = post(client, 'account', username=alias, email='alias@univ.edu')
    assert response.status_code == 400
    assert 'case' in response.json()['detail'].lower()
    assert auth.username_for_email('alias@univ.edu') is None
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM users ORDER BY username').fetchall() == before
        assert not conn.execute('SELECT 1 FROM group_exclusions WHERE username=?', (alias,)).fetchone()
