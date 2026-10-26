// MuscatRunConflict.launch(send, onRename) -> Promise<object>
//
// A launch under a run name that is already taken comes back as HTTP 409 with
// `conflict: "run_exists"` (issue #317). This asks the viewer what to do, in
// the same modal as other confirmations:
//
//   New run name [<suggested name>]  (editable, focused and selected)
//   Save as                          (primary, the default for Enter)
//   Overwrite                        (danger)
//   Cancel
//
// `send(extra)` posts the launch with `extra` merged into the request body and
// resolves to the parsed JSON. Save as calls `onRename(name)` with the name in
// the field (the server's suggestion unless the viewer typed another) so the
// page can put it in its run-name field, then sends again; a name that is
// taken too just asks again. Overwrite sends again with the conflict's
// `confirm_token`. Cancel resolves to `{ok: false, cancelled: true}` and
// launches nothing. A run the viewer may not see comes back with
// `can_overwrite: false` and no token: the popup then offers only a new name.
window.MuscatRunConflict = (function () {
  // Another conflict can follow (the typed name was taken too, or the run
  // changed owner meanwhile); stop asking after a few.
  var MAX_ROUNDS = 5;

  function day(iso) {
    return (iso || '').slice(0, 10);
  }

  function describe(c) {
    var name = '"' + c.run_name + '"';
    if (c.can_overwrite === false) {
      return {
        title: 'The run name ' + name + ' is taken',
        text: 'A run named ' + name + ' already exists for this target. Choose a new run name.',
        overwriteLabel: ''
      };
    }
    var made = '';
    if (c.created_at) {
      made = ', made by ' + (c.own_run ? 'you' : (c.owner || 'an unknown user')) + ' on ' + day(c.created_at);
    }
    var title = c.own_run
      ? 'You already have a run named ' + name
      : (c.owner ? 'This run belongs to ' + c.owner : 'A run named ' + name + ' already exists');
    var whose = c.own_run || !c.owner ? 'it' : c.owner + "'s results";
    var text = 'A run named ' + name + ' already exists for this target' + made + '. ' +
      'Overwriting replaces ' + whose + ' and cannot be undone. ' +
      'Save under a new run name to keep both runs.';
    var overwriteLabel = c.own_run || !c.owner ? 'Overwrite' : 'Overwrite ' + c.owner + "'s run";
    return { title: title, text: text, overwriteLabel: overwriteLabel };
  }

  // Resolves to {action: 'new', name}, {action: 'overwrite'} or null (cancelled).
  function ask(c) {
    var d = describe(c);
    return window.showConfirmModal(d.title, d.text, {
      inputLabel: 'New run name',
      inputValue: c.suggested_run_name || '',
      confirmLabel: 'Save as',
      altLabel: d.overwriteLabel,
      altClass: 'btn danger'
    }).then(function (answer) {
      if (answer.choice === 'alt') return { action: 'overwrite' };
      if (answer.choice === 'ok' && answer.value) return { action: 'new', name: answer.value };
      return null;
    });
  }

  function launch(send, onRename) {
    function attempt(extra, round) {
      return send(extra).then(function (res) {
        if (!res || res.conflict !== 'run_exists' || round >= MAX_ROUNDS) return res;
        return ask(res).then(function (answer) {
          if (answer && answer.action === 'overwrite' && res.confirm_token) {
            return attempt({ confirm_overwrite: res.confirm_token }, round + 1);
          }
          if (answer && answer.action === 'new') {
            onRename(answer.name);
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
