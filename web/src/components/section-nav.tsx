import type { HomepageCopy } from '@/lib/locales'

/**
 * The in-page section navigation.
 *
 * One landmark serves both breakpoints: a compact horizontal shortcut bar on a
 * narrow screen and a sticky list beside the content on a wide one, so the
 * document never carries two copies of the same navigation. The links are
 * plain anchors, so they work without JavaScript and keep the browser's
 * focus handling.
 */
export function SectionNav({ copy }: { copy: HomepageCopy }) {
  return (
    <nav className="section-nav" aria-label={copy.nav.label}>
      <ul className="section-nav-list">
        {copy.nav.sections.map((section) => (
          <li key={section.href}>
            <a className="section-nav-link" href={section.href}>
              {section.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}
