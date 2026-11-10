/* Load only nearby, visible products; animated originals are opt-in. */
(function () {
  function load(img) {
    if (!img.dataset.src) return;
    img.srcset = img.dataset.srcset || '';
    img.src = img.dataset.src;
    delete img.dataset.src;
    delete img.dataset.srcset;
  }
  const pending = document.querySelectorAll('.phot-image[data-src]');
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting && entry.target.getClientRects().length) {
          load(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, {rootMargin: '300px 0px'});
    pending.forEach(img => observer.observe(img));
  } else {
    // Native lazy loading retains a usable fallback in older browsers.
    pending.forEach(img => { img.loading = 'lazy'; load(img); });
  }
  document.querySelectorAll('.animation-toggle').forEach(button => {
    const img = button.closest('figure').querySelector('img');
    let poster;
    button.addEventListener('click', () => {
      load(img);
      if (button.getAttribute('aria-pressed') !== 'true') {
        poster = {src: img.src, srcset: img.srcset};
        img.removeAttribute('srcset');
        img.src = button.dataset.original;
        button.setAttribute('aria-pressed', 'true');
        button.textContent = 'Stop animation';
      } else {
        img.srcset = poster.srcset;
        img.src = poster.src;
        button.setAttribute('aria-pressed', 'false');
        button.textContent = 'Play animation';
      }
    });
  });
})();
