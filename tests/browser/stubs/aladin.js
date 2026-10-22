// Stand-in for Aladin Lite v3: enough of the API for fov.html to build its
// overlays; calls are recorded on A.__calls.
(function () {
  var calls = [];
  function layer(kind) {
    return {
      kind: kind,
      add: function () { calls.push({ fn: kind + '.add' }); },
      addFootprints: function () { calls.push({ fn: kind + '.addFootprints' }); },
      removeAll: function () { calls.push({ fn: kind + '.removeAll' }); },
      show: function () {}, hide: function () {}, isShowing: true,
    };
  }
  function aladin() {
    var ra = 0, dec = 0, fov = 1;
    return {
      gotoRaDec: function (r, d) { ra = r; dec = d; calls.push({ fn: 'gotoRaDec' }); },
      gotoObject: function () { calls.push({ fn: 'gotoObject' }); },
      getRaDec: function () { return [ra, dec]; },
      setFoV: function (f) { fov = f; }, setFov: function (f) { fov = f; },
      getFov: function () { return [fov, fov]; },
      addCatalog: function () { calls.push({ fn: 'addCatalog' }); },
      addOverlay: function () { calls.push({ fn: 'addOverlay' }); },
      removeLayers: function () {}, removeLayer: function () {},
      setImageSurvey: function () {}, setBaseImageLayer: function () {},
      newImageSurvey: function () { return {}; },
      on: function () {}, setRotation: function () {}, getSize: function () { return [500, 500]; },
      pix2world: function () { return [ra, dec]; }, world2pix: function () { return [250, 250]; },
      getShareURL: function () { return ''; },
    };
  }
  window.A = {
    init: Promise.resolve(),
    aladin: function () { calls.push({ fn: 'aladin' }); return aladin(); },
    graphicOverlay: function () { return layer('overlay'); },
    catalog: function () { return layer('catalog'); },
    source: function (ra, dec, data) { return { ra: ra, dec: dec, data: data }; },
    marker: function (ra, dec, data) { return { ra: ra, dec: dec, data: data }; },
    polygon: function () { return {}; }, polyline: function () { return {}; },
    circle: function () { return {}; }, line: function () { return {}; },
    footprintsFromSTCS: function () { return []; },
    __calls: calls,
  };
})();
