// Upload an external light curve on the transit-fit page (issue #382).
//
// The uploader picks a role for every column of the file(s); the server never
// reads meaning into column names. The server repeats every check made here:
// the client only keeps Save disabled until a mapping could be valid.
(function () {
  'use strict';

  var PRIMARY_ROLES = [
    ['', 'ignore'], ['time', 'time'], ['flux', 'flux'], ['err', 'error'], ['cov', 'covariate'],
  ];
  var ANCILLARY_ROLES = [['', 'ignore'], ['cov', 'covariate']];

  function init() {
    var root = document.getElementById('lc-upload');
    if (!root) return;
    var maxBytes = parseInt(root.dataset.maxBytes, 10) || 0;
    var el = function (id) { return document.getElementById(id); };
    var state = { primary: null, ancillary: null };

    function setMsg(text, kind, link) {
      var msg = el('lcu-msg');
      msg.className = 'lco-msg' + (kind ? ' ' + kind : '');
      msg.textContent = text;
      if (link) {
        msg.appendChild(document.createTextNode(' '));
        var a = document.createElement('a');
        a.href = link.href;
        a.textContent = link.text;
        msg.appendChild(a);
      }
    }

    function readBase64(file) {
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () {
          var url = String(reader.result || '');
          resolve(url.slice(url.indexOf(',') + 1));
        };
        reader.onerror = function () { reject(new Error('could not read ' + file.name)); };
        reader.readAsDataURL(file);
      });
    }

    function post(url, body) {
      return window.MuscatFetch.fetchJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }, { submit: url.indexOf('/save') !== -1 });
    }

    function renderMap(which, info) {
      var host = el('lcu-' + which + '-map');
      host.textContent = '';
      if (!info) return;
      var roles = which === 'primary' ? PRIMARY_ROLES : ANCILLARY_ROLES;
      var title = document.createElement('h3');
      title.style.margin = '0.5rem 0';
      title.textContent = info.name + ' (' + info.n_rows + ' rows)';
      host.appendChild(title);
      // The join key is chosen apart from the roles, so the time column can
      // also be the key. Shown only while a second file is loaded.
      var joinLabel = document.createElement('label');
      joinLabel.className = 'lcu-join-label';
      joinLabel.style.cssText = 'font-size:0.85rem;color:var(--text-dim);display:none;' +
        'gap:0.5rem;align-items:center;margin-bottom:0.5rem;';
      joinLabel.appendChild(document.createTextNode('Join on'));
      var join = document.createElement('select');
      join.className = 'lcu-join';
      join.dataset.which = which;
      [''].concat(info.columns).forEach(function (col) {
        var o = document.createElement('option');
        o.value = col;
        o.textContent = col || '-- choose key column --';
        join.appendChild(o);
      });
      join.addEventListener('change', validate);
      joinLabel.appendChild(join);
      host.appendChild(joinLabel);
      var table = document.createElement('table');
      table.className = 'lcu-map';
      table.dataset.which = which;
      var head = table.createTHead().insertRow();
      ['Column', 'Role', 'First values'].forEach(function (h) {
        var th = document.createElement('th');
        th.textContent = h;
        head.appendChild(th);
      });
      var body = table.createTBody();
      info.columns.forEach(function (col, i) {
        var tr = body.insertRow();
        tr.insertCell().textContent = col;
        var sel = document.createElement('select');
        sel.className = 'lcu-role';
        sel.dataset.column = col;
        sel.setAttribute('aria-label', 'Role of column ' + col);
        roles.forEach(function (r) {
          var o = document.createElement('option');
          o.value = r[0];
          o.textContent = r[1];
          sel.appendChild(o);
        });
        sel.addEventListener('change', validate);
        tr.insertCell().appendChild(sel);
        var sample = info.preview.slice(0, 3).map(function (row) { return row[i]; }).join(', ');
        var td = tr.insertCell();
        td.className = 'mono';
        td.textContent = sample;
      });
      host.appendChild(table);
    }

    function rolesOf(which) {
      var out = { time: [], flux: [], err: [], cov: [] };
      document.querySelectorAll('table.lcu-map[data-which="' + which + '"] select.lcu-role')
        .forEach(function (sel) { if (sel.value) out[sel.value].push(sel.dataset.column); });
      return out;
    }

    // The mapping the server expects, or a reason it is not ready yet.
    function buildMapping() {
      if (!state.primary) return { error: 'Choose a light curve file.' };
      var p = rolesOf('primary');
      var labels = { time: 'time', flux: 'flux', err: 'error' };
      for (var role in labels) {
        if (p[role].length !== 1) return { error: 'Pick exactly one ' + labels[role] + ' column.' };
      }
      var mapping = {
        time: p.time[0], flux: p.flux[0], err: p.err[0],
        time_system: el('lcu-time-system').value,
        covariates: p.cov, ancillary_covariates: [], join_primary: '', join_ancillary: '',
      };
      if (state.ancillary) {
        var a = rolesOf('ancillary');
        var keyP = joinOf('primary');
        var keyA = joinOf('ancillary');
        if (!keyP || !keyA) return { error: 'Choose the column to join on in each file.' };
        if (!a.cov.length) return { error: 'Pick at least one covariate from the second file.' };
        if (a.cov.indexOf(keyA) !== -1) return { error: 'The join key cannot also be a covariate.' };
        mapping.join_primary = keyP;
        mapping.join_ancillary = keyA;
        mapping.ancillary_covariates = a.cov;
      }
      return { mapping: mapping };
    }

    function joinOf(which) {
      var sel = document.querySelector('select.lcu-join[data-which="' + which + '"]');
      return sel ? sel.value : '';
    }

    function showJoins() {
      document.querySelectorAll('.lcu-join-label').forEach(function (label) {
        label.style.display = state.ancillary ? 'flex' : 'none';
      });
    }

    function validate() {
      showJoins();
      var built = buildMapping();
      el('lcu-save').disabled = !!built.error;
      setMsg(built.error || '', '');
      return built;
    }

    function loadFile(which) {
      var input = el('lcu-' + which);
      var file = input.files && input.files[0];
      state[which] = null;
      renderMap(which, null);
      if (!file) { validate(); return; }
      if (maxBytes && file.size > maxBytes) {
        setMsg(file.name + ' is larger than ' + Math.round(maxBytes / 1048576) + ' MB.', 'err');
        input.value = '';
        validate();
        return;
      }
      setMsg('Reading ' + file.name + '…', '');
      readBase64(file)
        .then(function (b64) {
          return post('/api/lc-upload/inspect', { file: { name: file.name, content_b64: b64 } })
            .then(function (info) {
              if (!info.ok) throw new Error(info.error || 'could not read the file');
              state[which] = { name: file.name, content_b64: b64 };
              renderMap(which, info);
              validate();
            });
        })
        .catch(function (err) {
          input.value = '';
          setMsg(err.message, 'err');
          el('lcu-save').disabled = true;
        });
    }

    function save() {
      var built = validate();
      if (built.error) return;
      var payload = {
        target: el('lcu-target').value.trim(),
        date: el('lcu-date').value.trim(),
        facility: el('lcu-facility').value.trim(),
        band: el('lcu-band').value,
        mapping: built.mapping,
        primary: state.primary,
      };
      if (state.ancillary) payload.ancillary = state.ancillary;
      el('lcu-save').disabled = true;
      setMsg('Saving…', '');
      post('/api/lc-upload/save', payload)
        .then(function (res) {
          if (!res.ok) throw new Error(res.error || 'upload refused');
          var href = '/transit-fit?inst=' + encodeURIComponent(res.inst) +
            '&date=' + encodeURIComponent(res.date) + '&target=' + encodeURIComponent(res.target);
          var note = res.warnings && res.warnings.length ? ' Note: ' + res.warnings.join('; ') + '.' : '';
          setMsg('Saved ' + res.file + '.' + note, 'ok', { href: href, text: 'Open it in the transit fit' });
        })
        .catch(function (err) {
          setMsg(err.message, 'err');
          el('lcu-save').disabled = false;
        });
    }

    function deleteUpload(button) {
      var box = el('lcu-existing');
      var name = button.getAttribute('aria-label').replace('Delete upload ', '');
      window.showConfirmModal('Delete upload', 'Delete ' + name + '? It can be uploaded again afterwards.',
        { confirmLabel: 'Delete', confirmClass: 'btn primary' }).then(function (ok) {
        if (!ok) return;
        button.disabled = true;
        post('/api/lc-upload/delete', {
          date: box.dataset.date, target: box.dataset.target, run_id: button.dataset.runId,
        })
          .then(function (res) {
            if (!res.ok) throw new Error(res.error || 'delete refused');
            window.location.reload();
          })
          .catch(function (err) {
            setMsg(err.message, 'err');
            button.disabled = false;
          });
      });
    }

    var existing = el('lcu-existing');
    if (existing) {
      existing.addEventListener('click', function (ev) {
        var button = ev.target.closest('.lcu-delete');
        if (button) deleteUpload(button);
      });
    }

    el('lcu-primary').addEventListener('change', function () { loadFile('primary'); });
    el('lcu-ancillary').addEventListener('change', function () { loadFile('ancillary'); });
    el('lcu-time-system').addEventListener('change', validate);
    el('lcu-save').addEventListener('click', save);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
