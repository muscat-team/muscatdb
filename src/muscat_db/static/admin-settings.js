(() => {
  const root = document.getElementById('admin-dashboard');
  if (!root) return;
  const message = document.getElementById('admin-message');
  const content = document.getElementById('admin-content');
  let busy = false;
  const heading = root.querySelector('h3');
  heading.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); heading.click(); }
  });
  heading.addEventListener('click', () => setTimeout(() => heading.setAttribute('aria-expanded', String(!root.classList.contains('collapsed'))), 0));
  function options(select, values) {
    const previous = select.value;
    select.replaceChildren(...values.map(value => new Option(value, value)));
    if (values.includes(previous)) select.value = previous;
  }
  function row(table, values) {
    const tr = table.insertRow();
    values.forEach(value => { tr.insertCell().textContent = value == null ? '—' : String(value); });
    return tr;
  }
  async function api(url, body) {
    const response = await fetch(url, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.error || 'Request failed. Check the form and try again.');
    return data;
  }
  async function refresh() {
    const data = await api('/api/admin/state');
    document.getElementById('admin-summary').textContent = `${data.users.length} accounts · ${data.users.filter(user => user.email).length} Cloudflare mappings · ${data.groups.reduce((count, group) => count + group.grants.length, 0)} active grants`;
    const users = root.querySelector('#admin-users tbody'); users.replaceChildren();
    data.users.forEach(user => row(users, [user.username, user.email, user.admin ? 'Admin: all observations' : user.scoped ? 'Explicit grants only' : 'Includes everyone', user.last_login]));
    const names = data.users.filter(user => !user.admin).map(user => user.username);
    root.querySelectorAll('[data-users]').forEach(select => options(select, names));
    const selectedAccount = root.querySelector('#admin-account');
    function showScope() {
      const user = data.users.find(user => user.username === selectedAccount.value);
      root.querySelector('#admin-scope').value = String(Boolean(user && user.scoped));
    }
    selectedAccount.onchange = showScope;
    showScope();
    const custom = data.groups.filter(group => !group.builtin && !group.name.startsWith('user:')).map(group => group.name);
    root.querySelectorAll('[data-groups]').forEach(select => options(select, select.dataset.groups === 'custom' ? custom : [...new Set(['everyone', ...custom, ...names.map(name => 'user:' + name)])]));
    const instrument = root.querySelector('#admin-instrument');
    options(instrument, ['', ...data.instruments]); instrument.options[0].textContent = 'Any instrument';
    const memberships = root.querySelector('#admin-memberships'); memberships.replaceChildren();
    data.groups.filter(group => custom.includes(group.name)).forEach(group => {
      const p = document.createElement('p'); p.textContent = `${group.name}: ${group.members.join(', ') || 'No members'}`; memberships.append(p);
    });
    const grants = root.querySelector('#admin-grants tbody'); grants.replaceChildren();
    data.groups.forEach(group => group.grants.forEach(grant => {
      const fields = ['proposal_id', 'instrument', 'target', 'date_from', 'date_to'].filter(key => grant[key] != null).map(key => `${key}=${grant[key]}`).join(', ');
      const tr = row(grants, [group.name, fields || 'All observations', grant.granted_by, grant.note]);
      const button = document.createElement('button'); button.type = 'button'; button.className = 'btn'; button.textContent = 'Revoke';
      button.addEventListener('click', () => {
        if (confirm(`Revoke grant #${grant.id} from ${group.name}? Other grants may still provide access.`)) save({action: 'revoke', grant_id: grant.id});
      }); tr.insertCell().append(button);
    }));
    content.hidden = false;
  }
  async function save(body) {
    if (busy) return;
    busy = true;
    root.querySelectorAll('button').forEach(button => button.disabled = true);
    let saved = false;
    try {
      await api('/api/admin/change', body); saved = true;
      await refresh();
      message.textContent = 'Saved. Cloudflare Access policy changes must be made separately in Cloudflare.';
    } catch (error) { message.textContent = (saved ? 'Saved, but refresh failed: ' : '') + error.message; }
    finally { busy = false; root.querySelectorAll('button').forEach(button => button.disabled = false); }
  }
  root.querySelectorAll('form').forEach(form => form.addEventListener('submit', event => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    body.action = event.submitter.value || form.dataset.adminAction;
    if (body.action === 'scope') body.scoped = body.scoped === 'true';
    if (['clear_email', 'scope', 'leave'].includes(body.action) && !confirm('Apply this access change to ' + body.username + '?')) return;
    if (body.action === 'grant' && body.group === 'everyone' && !confirm('Grant these observations to everyone, except accounts limited to explicit grants?')) return;
    save(body);
  }));
  document.getElementById('admin-refresh').addEventListener('click', () => refresh().then(() => { message.textContent = 'Updated.'; }).catch(error => { message.textContent = error.message; }));
  refresh().then(() => { message.textContent = 'Administration loaded.'; }).catch(error => { message.textContent = error.message; });
})();
