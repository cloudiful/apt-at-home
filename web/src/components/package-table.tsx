import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { HomepageCopy } from '@/lib/locales'
import { exactReleaseDate, relativeReleaseLabel } from '@/lib/release-date'
import type { PackageSummary } from '@/lib/types'

/**
 * The published-package table, or the status shown when the catalog could not
 * be read.
 *
 * The table is a real table with a caption and header scopes, and it scrolls
 * inside its own container on a narrow screen instead of widening the page.
 * The caption names the table for assistive technology and stays out of the
 * visible text, where the section heading already labels it. The upstream
 * release instant is formatted from the canonical value, never from the
 * browser's locale or clock, so the server and the browser agree.
 */
export function PackageTable({
  packages,
  copy,
  now,
}: {
  packages: readonly PackageSummary[] | null
  copy: HomepageCopy
  now: string
}) {
  if (packages === null || packages.length === 0) {
    return (
      <p className="text-muted-foreground" role="status">
        {copy.catalog.unavailable}
      </p>
    )
  }

  return (
    <Table>
      <TableCaption className="sr-only">{copy.catalog.caption}</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">{copy.catalog.packageColumn}</TableHead>
          <TableHead scope="col">{copy.catalog.versionColumn}</TableHead>
          <TableHead scope="col">{copy.catalog.architectureColumn}</TableHead>
          <TableHead scope="col">{copy.catalog.publishedAtColumn}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {packages.map((pkg) => (
          <TableRow key={pkg.name}>
            <TableHead scope="row" className="font-mono">
              {pkg.name}
            </TableHead>
            <TableCell>{pkg.version}</TableCell>
            <TableCell>{pkg.architectures.join(', ')}</TableCell>
            <TableCell>{publishedAtCell(pkg, copy, now)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** The release date cell: relative age first, then the UTC day, or an em dash when unknown. */
function publishedAtCell(pkg: PackageSummary, copy: HomepageCopy, now: string) {
  const publishedAt = pkg.upstreamPublishedAt
  if (publishedAt === undefined) {
    return copy.catalog.publishedAtUnknown
  }
  const exact = exactReleaseDate(publishedAt)
  const relative = relativeReleaseLabel(publishedAt, now, copy.catalog)
  if (relative === null) {
    return <time dateTime={publishedAt}>{exact}</time>
  }
  return (
    <span className="release-date">
      <span>{relative}</span>
      <span aria-hidden="true"> · </span>
      <time dateTime={publishedAt}>{exact}</time>
    </span>
  )
}
