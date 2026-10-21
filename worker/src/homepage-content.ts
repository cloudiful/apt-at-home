/**
 * Landing-page content dispatcher.
 *
 * Keeps the shared constants, the localized copy contract, and the assembly of
 * one localized document body from the per-locale prose modules plus the shared
 * command blocks. The only value taken from the request is the origin, and it
 * is escaped inside the command builder before interpolation, so a crafted
 * hostname can never inject markup into the page.
 *
 * The package table is rendered from the published catalog: every value that
 * reaches the page is escaped, and the install commands next to it are built
 * from the same catalog, so the table and the commands can never disagree.
 */
import { escapeHtml, renderAuthDetails, renderInstallSteps } from './homepage-commands.js';
import { homepageCopy as en } from './homepage-content.en.js';
import { homepageCopy as zh } from './homepage-content.zh.js';
import type { InstallStepsCopy } from './homepage-commands.js';
import type { PackageSummary } from './packages.js';
import type { Locale } from './i18n.js';

export { escapeHtml } from './homepage-commands.js';

/** Public repository the guide links to; carries no account or bucket detail. */
export const REPOSITORY_URL = 'https://github.com/cloudiful/apt-at-home';

/** Platform the published packages target; the same in every locale. */
export const PLATFORM_LABEL = 'Debian 13';

/** Inline favicon so the page needs no external asset or network fetch. */
export const FAVICON_DATA_URI =
  "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'%20fill='%232563eb'/%3E%3Ctext%20x='16'%20y='23'%20font-family='monospace'%20font-size='19'%20font-weight='700'%20fill='%23ffffff'%20text-anchor='middle'%3EA%3C/text%3E%3C/svg%3E";

/** The prose contract every locale module satisfies. */
export interface HomepageCopy {
  readonly languageLabel: string;
  readonly catalog: CatalogCopy;
  readonly stepsHeading: string;
  readonly steps: InstallStepsCopy;
  readonly footer: {
    readonly linkText: string;
  };
}

/** The prose contract for the published-package section. */
export interface CatalogCopy {
  readonly heading: string;
  readonly caption: string;
  readonly packageColumn: string;
  readonly versionColumn: string;
  readonly architectureColumn: string;
  readonly unavailable: string;
}

const COPY: Record<Locale, HomepageCopy> = { en, zh };

const LOCALE_LINKS = {
  en: { label: 'English', lang: 'en' },
  zh: { label: '中文', lang: 'zh-CN' },
} as const;

/**
 * Renders the page body for one locale, request origin and published catalog.
 *
 * The locale switch links are normalized to `?lang=en`/`?lang=zh` with the
 * current one marked `aria-current`, so the query string never reflects
 * arbitrary input. Without a catalog the section shows a short status
 * instead of a table.
 */
export function renderHomepageBody(
  origin: string,
  locale: Locale,
  packages?: readonly PackageSummary[],
): string {
  const copy = COPY[locale];
  return `<main>
  <header>
    <h1>apt-at-home</h1>
    <p class="platform">${PLATFORM_LABEL}</p>
    ${renderLocaleSwitch(locale, copy.languageLabel)}
  </header>

  <section aria-labelledby="catalog">
    <h2 id="catalog">${copy.catalog.heading}</h2>
${renderCatalog(packages, copy.catalog)}
  </section>

  <section aria-labelledby="steps">
    <h2 id="steps">${copy.stepsHeading}</h2>
    <ol class="steps">
${renderInstallSteps(origin, copy.steps, packages)}
    </ol>
${renderAuthDetails(origin, copy.steps.auth)}
  </section>

  <footer>
    <p><a href="${REPOSITORY_URL}">${copy.footer.linkText}</a></p>
  </footer>
</main>
`;
}

/**
 * Renders the published-package table, or the status shown when the catalog
 * could not be read.
 *
 * The table is a real table with a caption and header scopes, and it scrolls
 * inside its own container on a narrow screen instead of widening the page.
 * The caption names the table for assistive technology and stays out of the
 * visible text, where the section heading already labels it.
 */
function renderCatalog(
  packages: readonly PackageSummary[] | undefined,
  copy: CatalogCopy,
): string {
  if (packages === undefined || packages.length === 0) {
    return `    <p class="status" role="status">${copy.unavailable}</p>`;
  }

  const rows = packages
    .map(
      (pkg) =>
        `        <tr><th scope="row">${escapeHtml(pkg.name)}</th>` +
        `<td>${escapeHtml(pkg.version)}</td>` +
        `<td>${escapeHtml(pkg.architectures.join(', '))}</td></tr>`,
    )
    .join('\n');

  return `    <div class="table-scroll">
      <table>
        <caption class="sr-only">${copy.caption}</caption>
        <thead>
          <tr><th scope="col">${copy.packageColumn}</th><th scope="col">${copy.versionColumn}</th><th scope="col">${copy.architectureColumn}</th></tr>
        </thead>
        <tbody>
${rows}
        </tbody>
      </table>
    </div>`;
}

function renderLocaleSwitch(locale: Locale, label: string): string {
  const links = (Object.keys(LOCALE_LINKS) as Locale[]).map((candidate) => {
    const { label: text, lang } = LOCALE_LINKS[candidate];
    const current = candidate === locale ? ' aria-current="true"' : '';
    return `<a href="?lang=${candidate}" hreflang="${lang}" lang="${lang}"${current}>${text}</a>`;
  });
  return `<nav class="locale" aria-label="${label}">${links.join('')}</nav>`;
}
