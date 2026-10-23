import * as React from 'react'
import { Check, Copy, TriangleAlert } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { CommandCopy } from '@/lib/locales'

type CopyState = 'idle' | 'copied' | 'failed'

/** How long a copy result stays visible before the control resets. */
const FEEDBACK_RESET_MS = 2000

export function createCopyFeedback(update: (state: CopyState) => void) {
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  return {
    show(result: Exclude<CopyState, 'idle'>) {
      if (disposed) return
      clearTimeout(timer)
      update(result)
      timer = setTimeout(() => update('idle'), FEEDBACK_RESET_MS)
    },
    dispose() {
      disposed = true
      clearTimeout(timer)
    },
  }
}

/**
 * One shell command with its copy control.
 *
 * The block renders the command as selectable text and copies exactly that
 * string, never the wrapped presentation. Each block owns its own feedback, so
 * copying one command never changes the label of another.
 */
export function CommandBlock({ command, labels }: { command: string; labels: CommandCopy }) {
  const [state, setState] = React.useState<CopyState>('idle')

  const feedback = React.useRef<ReturnType<typeof createCopyFeedback> | null>(null)

  React.useEffect(() => {
    const controller = createCopyFeedback(setState)
    feedback.current = controller
    return () => controller.dispose()
  }, [])

  const handleCopy = React.useCallback(async () => {
    try {
      if (typeof navigator === 'undefined' || navigator.clipboard === undefined) {
        feedback.current?.show('failed')
        return
      }
      await navigator.clipboard.writeText(command)
      feedback.current?.show('copied')
    } catch {
      feedback.current?.show('failed')
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
