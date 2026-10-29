// fetchJson(url, options, opts) -> Promise<object>
//
// fetch() + resp.json() that never leaks a raw parser error. When a proxy or
// the server answers with HTML (e.g. an nginx 504 for a slow upstream call),
// resp.json() throws "Unexpected token '<' ... is not valid JSON", which tells
// the user nothing. This rejects with an Error carrying a readable message.
//
// `opts.submit` marks a mutating request (booking telescope time). There, a
// 502/503/504 does not mean the request failed: the proxy can time out after
// the request reached LCO, so the message must not invite a blind retry that
// could double-book. Reads and planning calls keep the "try again" wording.
window.MuscatFetch = (function () {
  function messageForStatus(status, opts) {
    opts = opts || {};
    if (status === 401 || status === 403) {
      return 'Your session has expired or you lack access. Reload the page and sign in again.';
    }
    if (status === 502 || status === 503 || status === 504) {
      if (opts.submit) {
        return 'The server did not answer in time. The request may have been submitted: ' +
          'check Monitored requests before trying again.';
      }
      return 'The server took too long to respond or is restarting. Wait a moment and try again; ' +
        'the result of a repeated request is usually much faster.';
    }
    if (status >= 500) {
      return 'The server hit an unexpected error (HTTP ' + status + '). Please try again.';
    }
    return 'The server returned an unexpected response (HTTP ' + status + ').';
  }

  function fetchJson(url, options, opts) {
    opts = opts || {};
    return fetch(url, options).then(
      function (resp) {
        return resp.text().then(function (text) {
          try {
            return JSON.parse(text);
          } catch (parseError) {
            throw new Error(messageForStatus(resp.status, opts));
          }
        });
      },
      function () {
        throw new Error('Could not reach the server. Check your connection and try again.');
      }
    );
  }

  // Read a complete archive response while displaying NDJSON progress events.
  async function readProgress(response, onProgress) {
    if (!(response.headers.get('Content-Type') || '').includes('application/x-ndjson')) {
      return response.json();
    }
    let result = null, pending = '';
    const reader = response.body.getReader(), decoder = new TextDecoder();
    function event(line) {
      if (!line.trim()) return;
      const message = JSON.parse(line);
      if (message.progress) onProgress(message.progress);
      else if (message.ok === false) throw new Error(message.error || 'Archive search failed');
      else if (message.ok) result = message;
    }
    try {
      while (true) {
        const chunk = await reader.read();
        pending += decoder.decode(chunk.value, {stream: !chunk.done});
        const lines = pending.split('\n'); pending = lines.pop();
        lines.forEach(event);
        if (chunk.done) break;
      }
      event(pending);
    } finally { await reader.cancel(); }
    if (!result) throw new Error('Archive search ended before completion');
    return result;
  }

  return { fetchJson: fetchJson, messageForStatus: messageForStatus, readProgress: readProgress };
})();
