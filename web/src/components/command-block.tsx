import * as React from 'react'
import { Check, Copy, TriangleAlert } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { CommandCopy } from '@/lib/locales'

type CopyState = 'idle' | 'copied' | 'failed'

/** How long a copy result stays visible before the control resets. */
const FEEDBACK_RESET_MS = 2000

/**
 * One shell command with its copy control.
 *
 * The block renders the command as selectable text and copies exactly that
 * string, never the wrapped presentation. Each block owns its own feedback, so
 * copying one command never changes the label of another.
 */
export function CommandBlock({ command, labels }: { command: string; labels: CommandCopy }) {
  const [state, setState] = React.useState<CopyState>('idle')

  React.useEffect(() => {
    if (state === 'idle') return
    const timer = setTimeout(() => setState('idle'), FEEDBACK_RESET_MS)
    return () => clearTimeout(timer)
  }, [state])

  const handleCopy = React.useCallback(async () => {
    try {
      if (typeof navigator === 'undefined' || navigator.clipboard === undefined) {
        setState('failed')
        return
      }
      await navigator.clipboard.writeText(command)
      setState('copied')
    } catch {
      setState('failed')
    }
  }, [command])

  const label =
    state === 'copied' ? labels.copied : state === 'failed' ? labels.failed : labels.copy
  const announcement = state === 'copied' ? labels.copied : state === 'failed' ? labels.failed : ''

  return (
    <div className="command-block">
      <pre className="code-block">
        <code>{command}</code>
      </pre>
      <div className="command-controls">
        <Button type="button" variant="outline" size="xs" onClick={handleCopy}>
          {state === 'copied' ? (
            <Check aria-hidden="true" />
          ) : state === 'failed' ? (
            <TriangleAlert aria-hidden="true" />
          ) : (
            <Copy aria-hidden="true" />
          )}
          <span>{label}</span>
        </Button>
        <span className="sr-only" role="status" aria-live="polite">
          {announcement}
        </span>
      </div>
    </div>
  )
}
