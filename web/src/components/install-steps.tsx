import * as React from 'react'

import { CommandBlock } from '@/components/command-block'
import {
  authCommands,
  installCommands,
  keyCommand,
  sourcesCommand,
} from '@/lib/catalog'
import type { HomepageCopy } from '@/lib/locales'
import type { PackageSummary } from '@/lib/types'

/**
 * The ordered installation steps.
 *
 * Each step is a title and its commands: the commands say what to run, so no
 * step repeats itself in an introductory paragraph. The command blocks are the
 * same in every locale apart from the request origin, so the two locales show
 * exactly the same commands.
 */
export function InstallSteps({
  origin,
  packages,
  copy,
}: {
  origin: string
  packages: readonly PackageSummary[] | null
  copy: HomepageCopy
}) {
  return (
    <ol className="steps">
      <li>
        <h3 className="font-medium">{copy.steps.key.title}</h3>
        <CommandBlock command={keyCommand(origin)} labels={copy.commands} />
        <details className="more">
          <summary>{copy.steps.key.fingerprintSummary}</summary>
          <p>{copy.steps.key.fingerprintNote}</p>
          <CommandBlock
            command="gpg --show-keys /etc/apt/keyrings/apt-at-home.asc"
            labels={copy.commands}
          />
        </details>
      </li>
      <li>
        <h3 className="font-medium">{copy.steps.sources.title}</h3>
        <CommandBlock command={sourcesCommand(origin)} labels={copy.commands} />
      </li>
      <li>
        <h3 className="font-medium">{copy.steps.install.title}</h3>
        {installCommands(packages, {
          amd64: copy.steps.install.amd64Intro,
          arm64: copy.steps.install.arm64Intro,
        }).map((block) => (
          <div key={block.command}>
            {block.label ? <p className="my-2">{block.label}</p> : null}
            <CommandBlock command={block.command} labels={copy.commands} />
          </div>
        ))}
      </li>
    </ol>
  )
}

/**
 * The collapsed device-credential guidance.
 *
 * The repository serves downloads anonymously while the credential gate stays
 * disabled. This block is collapsed by default and only documents the extra
 * steps that become necessary if the operator re-enables authentication; it
 * never carries a real token, only placeholders.
 */
export function AuthDetails({ origin, copy }: { origin: string; copy: HomepageCopy }) {
  const [credential = '', machine = '', curl = ''] = authCommands(origin)

  return (
    <details className="more">
      <summary>{copy.steps.auth.summary}</summary>
      <p>{copy.steps.auth.intro}</p>
      <CommandBlock command={credential} labels={copy.commands} />
      <p>{keywordProse(copy.steps.auth.fileLabel)}</p>
      <CommandBlock command={machine} labels={copy.commands} />
      <p>{copy.steps.auth.curlNote}</p>
      <CommandBlock command={curl} labels={copy.commands} />
    </details>
  )
}

/**
 * Renders prose that names the `machine` keyword as code.
 *
 * The keyword is marked in the copy, so both locales show it the same way
 * without either of them carrying markup.
 */
function keywordProse(text: string): React.ReactNode {
  return text.split('{machine}').flatMap((part, index) =>
    index === 0 ? [part] : [<code key={index}>machine</code>, part],
  )
}
