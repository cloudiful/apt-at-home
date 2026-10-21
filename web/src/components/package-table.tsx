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
import type { PackageSummary } from '@/lib/types'

/**
 * The published-package table, or the status shown when the catalog could not
 * be read.
 *
 * The table is a real table with a caption and header scopes, and it scrolls
 * inside its own container on a narrow screen instead of widening the page.
 * The caption names the table for assistive technology and stays out of the
 * visible text, where the section heading already labels it.
 */
export function PackageTable({
  packages,
  copy,
}: {
  packages: readonly PackageSummary[] | null
  copy: HomepageCopy
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
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
