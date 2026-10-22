// Stand-in for cdn.socket.io: a socket that never connects, so the chat widget
// in base.html loads without a websocket server.
(function () {
  window.io = function () {
    var handlers = {};
    var socket = {
      connected: false,
      id: null,
      on: function (ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); return socket; },
      once: function (ev, fn) { return socket.on(ev, fn); },
      off: function (ev) { delete handlers[ev]; return socket; },
      emit: function () { return socket; },
      connect: function () { return socket; },
      disconnect: function () { return socket; },
      close: function () { return socket; },
      io: { on: function () {}, opts: {} },
    };
    return socket;
  };
})();
