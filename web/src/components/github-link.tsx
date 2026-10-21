import type { HomepageCopy } from '@/lib/locales'

/** Public repository the page links to; carries no account or bucket detail. */
export const REPOSITORY_URL = 'https://github.com/cloudiful/apt-at-home'

/** Ready-made stars badge for the public repository. */
export const STARS_IMAGE_URL =
  'https://img.shields.io/github/stars/cloudiful/apt-at-home?style=social'

/**
 * The link to the public repository.
 *
 * The icon and the badge sit inside one link, so a badge that fails to load
 * never hides the repository link. The badge carries a short alternative text
 * and the link carries the repository name for assistive technology.
 */
export function GitHubLink({ copy }: { copy: HomepageCopy }) {
  return (
    <a
      href={REPOSITORY_URL}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-2 rounded-md px-2 py-2 text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
    >
      <GitHubMark />
      <img
        src={STARS_IMAGE_URL}
        alt={copy.githubStarsAlt}
        width={80}
        height={20}
        className="h-5 w-20"
      />
      <span className="sr-only">{copy.githubLabel}</span>
    </a>
  )
}

/**
 * The GitHub mark.
 *
 * The icon library ships no brand marks, so the mark is drawn here and stays
 * decorative: the link carries the repository name for assistive technology.
 */
function GitHubMark() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="currentColor"
      aria-hidden="true"
      className="size-4"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  )
}
