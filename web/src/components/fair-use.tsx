import { REQUEST_LIMITS } from '@/lib/locales'
import type { HomepageCopy } from '@/lib/locales'

/**
 * The per-IP request budgets, stated for a client.
 *
 * The numbers come from `REQUEST_LIMITS`, the same constant the deployment
 * test binds to the Worker's own rate-limit bindings, so the page cannot drift
 * from what the gateway enforces. The prose describes what a client may
 * request, not how the gateway counts or where its counters live.
 */
export function FairUse({ copy }: { copy: HomepageCopy }) {
  const rows = [
    { label: copy.fairUse.limits.index, value: `${REQUEST_LIMITS.index} ${copy.fairUse.unit}` },
    { label: copy.fairUse.limits.downloads, value: `${REQUEST_LIMITS.downloads} ${copy.fairUse.unit}` },
    { label: copy.fairUse.limits.perFile, value: `${REQUEST_LIMITS.perFile} ${copy.fairUse.unit}` },
  ]

  return (
    <section aria-labelledby="usage" className="mt-8">
      <h2 id="usage" className="mb-3 border-b pb-1.5 text-lg">
        {copy.fairUse.heading}
      </h2>
      <p>{copy.fairUse.summary}</p>
      <dl className="fair-use-limits">
        {rows.map((row) => (
          <div key={row.label} className="fair-use-row">
            <dt>{row.label}</dt>
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>
      <p>{copy.fairUse.retry}</p>
    </section>
  )
}
