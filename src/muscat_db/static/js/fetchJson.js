// fetchJson(url, options) -> Promise<object>
//
// fetch() + resp.json() that never leaks a raw parser error. When a proxy or
// the server answers with HTML (e.g. an nginx 504 for a slow upstream call),
// resp.json() throws "Unexpected token '<' ... is not valid JSON", which tells
// the user nothing. This rejects with an Error carrying a readable message.
window.MuscatFetch = (function () {
  function messageForStatus(status) {
    if (status === 401 || status === 403) {
      return 'Your session has expired or you lack access. Reload the page and sign in again.';
    }
    if (status === 502 || status === 503 || status === 504) {
      return 'The server took too long to respond or is restarting. Wait a moment and try again; ' +
        'the result of a repeated request is usually much faster.';
    }
    if (status >= 500) {
      return 'The server hit an unexpected error (HTTP ' + status + '). Please try again.';
    }
    return 'The server returned an unexpected response (HTTP ' + status + ').';
  }

  function fetchJson(url, options) {
    return fetch(url, options).then(
      function (resp) {
        return resp.text().then(function (text) {
          try {
            return JSON.parse(text);
          } catch (parseError) {
            throw new Error(messageForStatus(resp.status));
          }
        });
      },
      function () {
        throw new Error('Could not reach the server. Check your connection and try again.');
      }
    );
  }

  return { fetchJson: fetchJson, messageForStatus: messageForStatus };
})();
