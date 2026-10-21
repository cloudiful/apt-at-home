/**
 * The hashed names of the browser assets the page loads.
 *
 * The build writes a manifest next to the assets it produced, and this module
 * imports it, so the Worker links exactly the files the build emitted. The
 * names are hashed, so a changed asset never keeps the name of the old one.
 */
import manifest from '../dist/assets/.vite/manifest.json';

interface ManifestEntry {
  readonly file: string;
  readonly css?: readonly string[];
}

const entries = manifest as unknown as Record<string, ManifestEntry>;
const entry = entries['web/src/main.tsx'];

if (entry === undefined) {
  throw new Error('the web build manifest carries no client entry');
}

/** Public URL of the client script that hydrates the page. */
export const CLIENT_SCRIPT_URL = `/assets/${entry.file}`;

/** Public URLs of the stylesheets the client script needs. */
export const CLIENT_STYLE_URLS = (entry.css ?? []).map((file) => `/assets/${file}`);

/**
 * Public URL of the theme bootstrap.
 *
 * The script is a plain file in the asset directory, so the document can load
 * it as an external script in the head, before the first paint. Its name is
 * deliberately stable: it is the one asset every document links, and it must
 * be the same file in every build.
 */
export const THEME_INIT_URL = '/assets/theme-init.js';
