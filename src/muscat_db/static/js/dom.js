/* Shared DOM helpers.
 *
 * These were duplicated across templates, and the copies had drifted. Most
 * importantly there were three different escapeHtml implementations feeding
 * user-facing tables, two of which mangled ordinary values:
 *
 *   input       base.html   jobs.html   lco_schedule.html
 *   0           ''          '0'         '0'
 *   false       ''          'false'     'false'
 *   null        ''          ''          'null'
 *   undefined   ''          ''          'undefined'
 *
 * All three escaped the same five characters, so this was never an XSS
 * difference -- but a zero silently vanished on one page and the literal text
 * "null" was rendered on another. The jobs.html semantics below are correct in
 * every case and are now the single definition.
 *
 * Loaded globally from base.html, so every page has window.MuscatDom.
 */
(function (global) {
  'use strict';

  function el(id) {
    return document.getElementById(id);
  }

  /* Escape for interpolation into HTML. null/undefined become '', every other
     value (including 0 and false) is stringified first so it survives. */
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* xterm 256-color palette index -> hex. Covers the 16 basic colors, the
     6x6x6 color cube, and the 24-step grayscale ramp (the standard xterm
     layout) so any 38;5;N sequence a tool emits resolves to a real color. */
  function _xterm256ToHex(code) {
    var n = Number(code);
    if (!isFinite(n) || n < 0 || n > 255) return null;
    var basic = ['#000000', '#800000', '#008000', '#808000', '#000080', '#800080', '#008080', '#c0c0c0',
      '#808080', '#ff0000', '#00ff00', '#ffff00', '#0000ff', '#ff00ff', '#00ffff', '#ffffff'];
    if (n < 16) return basic[n];
    function hex2(v) { return ('0' + v.toString(16)).slice(-2); }
    if (n < 232) {
      var i = n - 16;
      var levels = [0, 95, 135, 175, 215, 255];
      return '#' + hex2(levels[Math.floor(i / 36) % 6]) + hex2(levels[Math.floor(i / 6) % 6]) + hex2(levels[i % 6]);
    }
    var gray = 8 + (n - 232) * 10;
    return '#' + hex2(gray) + hex2(gray) + hex2(gray);
  }

  /* Standard 8/16-color SGR foreground codes (30-37 normal, 90-97 bright). */
  var ANSI_STANDARD_FG = {
    30: '#000000', 31: '#800000', 32: '#008000', 33: '#808000',
    34: '#000080', 35: '#800080', 36: '#008080', 37: '#c0c0c0',
    90: '#808080', 91: '#ff0000', 92: '#00ff00', 93: '#ffff00',
    94: '#0000ff', 95: '#ff00ff', 96: '#00ffff', 97: '#ffffff'
  };

  /* Render ANSI SGR color escapes (as emitted by prose's console_utils, e.g.
     pipeline logs colorizing "INFO"/"WARNING"/"ERROR") as HTML instead of
     showing the raw escape bytes. The whole string is escaped first, so the
     only markup this ever introduces is the fixed-palette <span> tags built
     below -- untrusted log content can't inject anything through it. Codes
     this doesn't recognize (bg colors, underline, dim, ...) are dropped
     rather than breaking the output. */
  function ansiToHtml(text) {
    var openTags = 0;
    var out = escapeHtml(text).replace(/\x1b\[([0-9;]*)m/g, function (_, body) {
      var codes = body.split(';').filter(function (c) { return c !== ''; });
      if (codes.length === 0) codes = ['0'];
      var html = '';
      for (var i = 0; i < codes.length; i++) {
        var code = parseInt(codes[i], 10);
        if (code === 0) {
          while (openTags > 0) { html += '</span>'; openTags--; }
        } else if (code === 1) {
          html += '<span style="font-weight:bold">';
          openTags++;
        } else if (code === 38 && codes[i + 1] === '5' && codes[i + 2] !== undefined) {
          var hex = _xterm256ToHex(codes[i + 2]);
          if (hex) { html += '<span style="color:' + hex + '">'; openTags++; }
          i += 2;
        } else if (ANSI_STANDARD_FG[code]) {
          html += '<span style="color:' + ANSI_STANDARD_FG[code] + '">';
          openTags++;
        }
      }
      return html;
    });
    while (openTags > 0) { out += '</span>'; openTags--; }
    return out;
  }

  /* Two field readers, deliberately kept distinct: the LCO pages trim, while
     the photometry and transit-fit pages pass values through to pipeline
     arguments and have never trimmed. Unifying them would be a silent
     behaviour change on those pages, so each template picks the one it uses. */
  function val(id) {
    var e = el(id);
    return e ? e.value.trim() : '';
  }

  function valRaw(id) {
    var e = el(id);
    return e ? e.value : '';
  }

  function num(id) {
    var v = val(id);
    return v === '' ? null : Number(v);
  }

  function chk(id) {
    var e = el(id);
    return e ? !!e.checked : false;
  }

  function setVal(id, v) {
    var e = el(id);
    if (e && v !== undefined && v !== null) e.value = v;
  }

  function setChk(id, v) {
    var e = el(id);
    if (e) e.checked = !!v;
  }

  /* Status line used by the LCO pages: text plus a state class. */
  function msg(id, text, kind) {
    var e = el(id);
    if (!e) return;
    e.textContent = text || '';
    e.className = 'lco-msg' + (kind ? ' ' + kind : '');
  }

  global.MuscatDom = {
    el: el,
    escapeHtml: escapeHtml,
    ansiToHtml: ansiToHtml,
    val: val,
    valRaw: valRaw,
    num: num,
    chk: chk,
    setVal: setVal,
    setChk: setChk,
    msg: msg,
  };
})(window);
