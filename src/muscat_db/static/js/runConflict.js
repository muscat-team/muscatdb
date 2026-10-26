// MuscatRunConflict.launch(send, onRename) -> Promise<object>
//
// A launch under a run name that is already taken comes back as HTTP 409 with
// `conflict: "run_exists"` (issue #317). This asks the viewer what to do, in
// the same modal as other confirmations:
//
//   Save as <suggested name>  (primary, the default for Enter)
//   Overwrite                 (danger)
//   Cancel
//
// `send(extra)` posts the launch with `extra` merged into the request body and
// resolves to the parsed JSON. Save as calls `onRename(name)` so the page can
// put the name in its run-name field, then sends again; Overwrite sends again
// with the conflict's `confirm_token`. Cancel resolves to
// `{ok: false, cancelled: true}` and launches nothing.
window.MuscatRunConflict = (function () {
  // A second conflict can follow a first (the suggested name was taken in the
  // meantime, or the run changed owner); stop asking after a few.
  var MAX_ROUNDS = 3;

  function day(iso) {
    return (iso || '').slice(0, 10);
  }

  function describe(c) {
    var name = '"' + c.run_name + '"';
    var made = '';
    if (c.created_at) {
      made = ', made by ' + (c.own_run ? 'you' : (c.owner || 'an unknown user')) + ' on ' + day(c.created_at);
    }
    var title = c.own_run
      ? 'You already have a run named ' + name
      : (c.owner ? 'This run belongs to ' + c.owner : 'A run named ' + name + ' already exists');
    var whose = c.own_run || !c.owner ? 'it' : c.owner + "'s results";
    var text = 'A run named ' + name + ' already exists for this target' + made + '. ' +
      'Overwriting replaces ' + whose + ' and cannot be undone.';
    if (c.suggested_run_name) {
      text += ' Save as "' + c.suggested_run_name + '" to keep both runs.';
    }
    var overwriteLabel = c.own_run || !c.owner ? 'Overwrite' : 'Overwrite ' + c.owner + "'s run";
    return { title: title, text: text, overwriteLabel: overwriteLabel };
  }

  // Resolves to 'new', 'overwrite' or null (cancelled).
  function ask(c) {
    var d = describe(c);
    if (!c.suggested_run_name) {
      return window.showConfirmModal(d.title, d.text, {
        confirmLabel: d.overwriteLabel, confirmClass: 'btn danger'
      }).then(function (ok) { return ok ? 'overwrite' : null; });
    }
    return window.showConfirmModal(d.title, d.text, {
      confirmLabel: 'Save as "' + c.suggested_run_name + '"',
      altLabel: d.overwriteLabel,
      altClass: 'btn danger'
    }).then(function (choice) {
      if (choice === 'alt') return 'overwrite';
      return choice ? 'new' : null;
    });
  }

  function launch(send, onRename) {
    function attempt(extra, round) {
      return send(extra).then(function (res) {
        if (!res || res.conflict !== 'run_exists' || round >= MAX_ROUNDS) return res;
        return ask(res).then(function (choice) {
          if (choice === 'overwrite') {
            return attempt({ confirm_overwrite: res.confirm_token }, round + 1);
          }
          if (choice === 'new') {
            onRename(res.suggested_run_name);
            return attempt({}, round + 1);
          }
          return { ok: false, cancelled: true };
        });
      });
    }
    return attempt({}, 0);
  }

  return { launch: launch, describe: describe };
})();
