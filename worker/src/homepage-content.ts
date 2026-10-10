/**
 * Landing-page content dispatcher.
 *
 * Keeps the shared constants, the localized copy contract, and the assembly of
 * one localized document body from the per-locale prose modules plus the shared
 * command blocks. The only value taken from the request is the origin, and it
 * is escaped inside the command builder before interpolation, so a crafted
 * hostname can never inject markup into the page.
 */
import { renderAuthDetails, renderInstallSteps } from './homepage-commands.js';
import { homepageCopy as en } from './homepage-content.en.js';
import { homepageCopy as zh } from './homepage-content.zh.js';
import type { InstallStepsCopy } from './homepage-commands.js';
import type { Locale } from './i18n.js';

export { escapeHtml } from './homepage-commands.js';

/** Public repository the guide links to; carries no account or bucket detail. */
export const REPOSITORY_URL = 'https://github.com/cloudiful/apt-at-home';

/** Inline favicon so the page needs no external asset or network fetch. */
export const FAVICON_DATA_URI =
  "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'%20fill='%232563eb'/%3E%3Ctext%20x='16'%20y='23'%20font-family='monospace'%20font-size='19'%20font-weight='700'%20fill='%23ffffff'%20text-anchor='middle'%3EA%3C/text%3E%3C/svg%3E";

/** The prose contract every locale module satisfies. */
export interface HomepageCopy {
  readonly lede: string;
  readonly languageLabel: string;
  readonly prerequisites: {
    readonly heading: string;
    readonly body: string;
  };
  readonly stepsHeading: string;
  readonly steps: InstallStepsCopy;
  readonly footer: {
    readonly before: string;
    readonly linkText: string;
    readonly after: string;
  };
}

const COPY: Record<Locale, HomepageCopy> = { en, zh };

const LOCALE_LINKS = {
  en: { label: 'English', lang: 'en' },
  zh: { label: '中文', lang: 'zh-CN' },
} as const;

/**
 * Renders the page body for one locale and request origin.
 *
 * The locale switch links are normalized to `?lang=en`/`?lang=zh` with the
 * current one marked `aria-current`, so the query string never reflects
 * arbitrary input.
 */
export function renderHomepageBody(origin: string, locale: Locale): string {
  const copy = COPY[locale];
  return `<main>
  <header>
    <h1>apt-at-home</h1>
    <p class="lede">${copy.lede}</p>
    ${renderLocaleSwitch(locale, copy.languageLabel)}
  </header>

  <section aria-labelledby="prerequisites">
    <h2 id="prerequisites">${copy.prerequisites.heading}</h2>
    <p>${copy.prerequisites.body}</p>
  </section>

  <section aria-labelledby="steps">
    <h2 id="steps">${copy.stepsHeading}</h2>
    <ol class="steps">
${renderInstallSteps(origin, copy.steps)}
    </ol>
${renderAuthDetails(origin, copy.steps.auth)}
  </section>

  <footer>
    <p>${copy.footer.before}<a href="${REPOSITORY_URL}">${copy.footer.linkText}</a>${copy.footer.after}</p>
  </footer>
</main>
`;
}

function renderLocaleSwitch(locale: Locale, label: string): string {
  const links = (Object.keys(LOCALE_LINKS) as Locale[]).map((candidate) => {
    const { label: text, lang } = LOCALE_LINKS[candidate];
    const current = candidate === locale ? ' aria-current="true"' : '';
    return `<a href="?lang=${candidate}" hreflang="${lang}" lang="${lang}"${current}>${text}</a>`;
  });
  return `<nav class="locale" aria-label="${label}">${links.join('')}</nav>`;
}
