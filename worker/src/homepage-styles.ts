/**
 * Inline styles for the public root landing page.
 *
 * Kept as one cohesive token set so the page stays a single self-contained
 * document: no external stylesheet, no framework, and a light/dark pair driven
 * by the system preference. The gateway's Content-Security-Policy allows only
 * `style-src 'unsafe-inline'` for exactly this block.
 */
export const HOMEPAGE_STYLES = `:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #1f2328;
  --muted: #59636e;
  --border: #d1d7de;
  --accent: #2563eb;
  --code-bg: #f0f2f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --text: #e6edf3;
    --muted: #9198a1;
    --border: #30363d;
    --accent: #4493f8;
    --code-bg: #1f242c;
  }
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  padding: 2rem 1rem 3rem;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main {
  margin: 0 auto;
  max-width: 46rem;
  padding: 1.5rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
}
h1 {
  margin: 0 0 0.25rem;
  font-size: 1.75rem;
}
h2 {
  margin: 2rem 0 0.75rem;
  padding-bottom: 0.35rem;
  font-size: 1.2rem;
  border-bottom: 1px solid var(--border);
}
h3 {
  margin: 0 0 0.5rem;
  font-size: 1rem;
}
p {
  margin: 0.5rem 0;
}
.lede {
  margin: 0;
  color: var(--muted);
}
.steps {
  display: grid;
  gap: 1.5rem;
  margin: 0;
  padding-left: 1.25rem;
}
.steps > li {
  padding-left: 0.25rem;
}
pre {
  margin: 0.5rem 0;
  padding: 0.75rem;
  overflow-x: auto;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.875em;
}
pre code {
  font-size: 0.85rem;
}
a {
  color: var(--accent);
}
footer {
  margin-top: 2rem;
  color: var(--muted);
  font-size: 0.9rem;
}
@media (max-width: 480px) {
  body {
    padding: 1rem 0.5rem 2rem;
  }
  main {
    padding: 1rem;
    border-radius: 8px;
  }
  h1 {
    font-size: 1.4rem;
  }
}
`;
