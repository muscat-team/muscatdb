// Stand-in for cdn.plot.ly: records every call so tests can assert a plot was
// drawn (and with what) without the 3 MB library or the network.
(function () {
  var calls = [];
  function record(name) {
    return function (el, data, layout) {
      var node = typeof el === 'string' ? document.getElementById(el) : el;
      calls.push({ fn: name, id: node && node.id, traces: Array.isArray(data) ? data.length : null });
      if (node) {
        node.data = Array.isArray(data) ? data : (node.data || []);
        node.layout = layout || node.layout || {};
        node.on = node.on || function () {};
        node.removeAllListeners = node.removeAllListeners || function () {};
        node.emit = node.emit || function () {};
      }
      return Promise.resolve(node);
    };
  }
  window.Plotly = {
    newPlot: record('newPlot'),
    react: record('react'),
    plot: record('plot'),
    relayout: function (el) { calls.push({ fn: 'relayout' }); return Promise.resolve(el); },
    restyle: function (el) { calls.push({ fn: 'restyle' }); return Promise.resolve(el); },
    update: function (el) { calls.push({ fn: 'update' }); return Promise.resolve(el); },
    addTraces: function (el) { calls.push({ fn: 'addTraces' }); return Promise.resolve(el); },
    deleteTraces: function (el) { calls.push({ fn: 'deleteTraces' }); return Promise.resolve(el); },
    extendTraces: function (el) { calls.push({ fn: 'extendTraces' }); return Promise.resolve(el); },
    purge: function () { calls.push({ fn: 'purge' }); },
    downloadImage: function () { calls.push({ fn: 'downloadImage' }); return Promise.resolve(); },
    toImage: function () { return Promise.resolve('data:image/png;base64,'); },
    Plots: { resize: function () { calls.push({ fn: 'resize' }); } },
    Fx: { hover: function () {}, unhover: function () {} },
    d3: null,
    __calls: calls,
  };
})();
