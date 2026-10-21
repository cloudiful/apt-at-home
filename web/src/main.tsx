import { hydrateRoot } from 'react-dom/client'

import { App } from '@/app'
import type { AppProps } from '@/lib/types'
import '@/styles/app.css'

/**
 * The browser entry.
 *
 * The server rendered the page and left the props it used in the document, so
 * the browser hydrates exactly that tree instead of rendering a second one.
 * The identifier prefix matches the server's, so the two trees agree on every
 * generated identifier.
 */
const IDENTIFIER_PREFIX = 'apt'

const container = document.getElementById('app')
const payload = document.getElementById('app-props')

if (container !== null && payload !== null) {
  const props = JSON.parse(payload.textContent ?? 'null') as AppProps
  hydrateRoot(container, <App {...props} />, { identifierPrefix: IDENTIFIER_PREFIX })
}
