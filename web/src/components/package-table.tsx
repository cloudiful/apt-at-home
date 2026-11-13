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
import type { PackageSummary, UpdateStatus } from '@/lib/types'
import { CircleCheck, CircleHelp, CircleX } from 'lucide-react'

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
  updateStatus,
}: {
  packages: readonly PackageSummary[] | null
  copy: HomepageCopy
  now: string
  updateStatus?: UpdateStatus | null
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
          <TableHead scope="col" className="w-10"><span className="sr-only">{copy.catalog.updateColumn}</span></TableHead>
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
            <TableCell><UpdateIcon name={pkg.name} status={updateStatus} now={now} copy={copy} /></TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function UpdateIcon({ name, status, now, copy }: { name: string; status?: UpdateStatus | null; now: string; copy: HomepageCopy }) {
  const labels = copy.catalog
  const entry = status?.packages[name]
  const stale = status ? Date.parse(now) - Date.parse(status.checked_at) > 86_400_000 : false
  const healthy = !!entry && entry.outcome !== 'deferred' && !stale && status?.outcome !== 'failure'
  const deferred = entry?.outcome === 'deferred'
  const description = !status ? labels.updateUnknown
    : status.outcome === 'failure' ? labels.updateFailed
    : !entry ? labels.updateUnknown
    : deferred ? `${labels.updateDeferred}: ${entry.stage ? labels.updateStages[entry.stage] : ''}`
    : stale ? labels.updateStale
    : entry.outcome === 'updated' ? labels.updateUpdated : labels.updateChecked
  const detail = status ? `${description} · ${labels.updateTime}: ${status.checked_at}` : description
  const Icon = healthy ? CircleCheck : deferred ? CircleX : CircleHelp
  const icon = <Icon aria-hidden="true" size={17} className={healthy ? 'text-green-600' : deferred ? 'text-red-600' : 'text-muted-foreground'} />
  const className = 'update-status-control'
  return status ? (
    <span className="update-status-wrap"><a className={className} href={status.run_url} aria-label={detail}>{icon}<span role="tooltip" className="update-status-tooltip">{detail}</span></a></span>
  ) : (
    <span className="update-status-wrap"><span className={className} role="img" tabIndex={0} aria-label={detail}>{icon}<span role="tooltip" className="update-status-tooltip">{detail}</span></span></span>
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
