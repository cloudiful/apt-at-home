/**
 * Inline styles for the public root landing page.
 *
 * Kept as one cohesive token set so the page stays a single self-contained
 * document: no external stylesheet, no framework, and a light/dark pair driven
 * by the system preference. The gateway's Content-Security-Policy allows only
 * `style-src 'unsafe-inline'` for exactly this block.
 *
 * The step list and code blocks are bounded deliberately: the grid track and
 * every list item may shrink below their content, so an unbroken command line
 * scrolls inside its own code block instead of widening the whole document.
 */
export const HOMEPAGE_STYLES = `:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #1f2328;
  --muted: #59636e;
  --border: #d1d7de;
  --accent: #2563eb;
  --accent-contrast: #ffffff;
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
    --accent-contrast: #0d1117;
    --code-bg: #1f242c;
  }
}
* {
  box-sizing: border-box;
}
html {
  -webkit-text-size-adjust: 100%;
}
body {
  margin: 0;
  padding: 2rem 1rem 3rem;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  overflow-wrap: break-word;
}
main {
  margin: 0 auto;
  max-width: 44rem;
  padding: 1.5rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
}
h1 {
  margin: 0 0 0.25rem;
  font-size: 1.6rem;
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
.locale {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 0.9rem;
}
.locale a {
  padding: 0.15rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text);
  font-size: 0.85rem;
  text-decoration: none;
}
.locale a:hover {
  border-color: var(--accent);
}
.locale a[aria-current="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-contrast);
}
.steps {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1.5rem;
  margin: 0;
  padding-left: 1.25rem;
}
.steps > li {
  min-width: 0;
  padding-left: 0.25rem;
}
details {
  min-width: 0;
  margin: 0.75rem 0;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: 8px;
}
summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 0.9rem;
}
summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
details > p {
  margin: 0.5rem 0 0;
}
pre {
  margin: 0.5rem 0;
  padding: 0.75rem;
  max-width: 100%;
  overflow-x: auto;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.875em;
  overflow-wrap: anywhere;
}
pre code {
  font-size: 0.85rem;
  white-space: pre;
  overflow-wrap: normal;
}
a {
  color: var(--accent);
}
a:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
footer {
  margin-top: 2rem;
  color: var(--muted);
  font-size: 0.9rem;
}
@media (max-width: 480px) {
  body {
    padding: 1rem 0.75rem 2rem;
  }
  main {
    padding: 1rem;
    border-radius: 8px;
  }
  h1 {
    font-size: 1.35rem;
  }
  h2 {
    font-size: 1.1rem;
  }
  pre code {
    font-size: 0.78rem;
  }
}
`;
