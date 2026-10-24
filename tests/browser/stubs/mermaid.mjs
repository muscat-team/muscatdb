// Stand-in for mermaid's ESM build: marks diagrams as rendered instead of
// drawing them, so guide.html's diagram code runs offline. The config passed
// to initialize() is kept on window.__mermaidConfig for assertions.
function markRendered(nodes) {
  nodes.forEach((el) => el.setAttribute('data-processed', 'true'));
}

const mermaid = {
  initialize(config = {}) {
    window.__mermaidConfig = config;
    if (config.startOnLoad) {
      const run = () => markRendered(document.querySelectorAll('.mermaid'));
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
      else run();
    }
  },
  async run({ nodes, querySelector } = {}) {
    markRendered(nodes || document.querySelectorAll(querySelector || '.mermaid'));
  },
  async render(id) {
    return { svg: `<svg id="${id}"></svg>`, bindFunctions() {} };
  },
};
export default mermaid;
