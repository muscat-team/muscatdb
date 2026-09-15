window.MuscatJobPolling = (function () {
  function createTimeoutPoller(pollFn) {
    var timer = null;

    return {
      stop: function () {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
      },
      schedule: function (delayMs) {
        if (timer !== null) return;
        timer = setTimeout(function () {
          timer = null;
          pollFn();
        }, delayMs);
      },
      isScheduled: function () {
        return timer !== null;
      },
    };
  }

  // Wraps a job's `/log-stream` SSE endpoint (architecture issue #51, step 4)
  // so a page can watch one job's status/log pushed live instead of polling
  // it. `onStatus` receives the exact same JSON shape as the matching
  // `/status` endpoint, so callers can feed it straight into their existing
  // status handler unchanged.
  //
  // Falls back to `onFallback()` (once) if the browser has no EventSource, or
  // if the connection closes for good -- a genuine HTTP/proxy failure, not a
  // transient drop, which EventSource already retries on its own. A caller
  // that wants polling as a safety net passes a fallback that resumes its old
  // fetch-based poll loop.
  function createLogStream(url, onStatus, onFallback) {
    var es = null;
    var stopped = false;

    function handleError() {
      if (stopped) return;
      // CLOSED means the browser gave up (e.g. a non-2xx or non-SSE
      // response) and will not retry on its own; anything else is a
      // transient drop the browser is already reconnecting from.
      if (es && es.readyState === EventSource.CLOSED) {
        stop();
        if (onFallback) onFallback();
      }
    }

    function start() {
      if (typeof window.EventSource === 'undefined') {
        if (onFallback) onFallback();
        return;
      }
      es = new EventSource(url);
      es.onmessage = function (evt) {
        try {
          onStatus(JSON.parse(evt.data));
        } catch (e) {
          // Malformed payload for this tick; the server sends a fresh one on
          // the next status change, nothing to recover here.
        }
      };
      es.onerror = handleError;
    }

    function stop() {
      stopped = true;
      if (es) {
        es.close();
        es = null;
      }
    }

    return { start: start, stop: stop };
  }

  return {
    createTimeoutPoller: createTimeoutPoller,
    createLogStream: createLogStream,
  };
})();
