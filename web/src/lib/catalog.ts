/**
 * The installation commands the page shows.
 *
 * The commands are derived from the published catalog rather than written out:
 * the package names come from the manifest the publisher already validated,
 * and the reader only ever sees names the shell accepts as plain arguments.
 * A package that exists for one architecture only gets its own command, so the
 * page never suggests installing a package the architecture does not carry.
 */
import type { PackageSummary } from './types'

/** One command block, with the prose that introduces it when it needs one. */
export interface CommandBlock {
  readonly label?: string
  readonly command: string
}

/** The commands that install the repository signing key. */
export function keyCommand(origin: string): string {
  return `sudo install -d -m0755 /etc/apt/keyrings
sudo curl --fail --show-error --output /etc/apt/keyrings/apt-at-home.asc ${origin}/key.asc
sudo chmod 0644 /etc/apt/keyrings/apt-at-home.asc`
}

/** The command that writes the Deb822 source entry. */
export function sourcesCommand(origin: string): string {
  return `sudo tee /etc/apt/sources.list.d/apt-at-home.sources >/dev/null <<'EOF'
Types: deb
URIs: ${origin}
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/apt-at-home.asc
EOF`
}

/** The labels that introduce an architecture-specific command. */
export interface ArchitectureLabels {
  readonly amd64: string
  readonly arm64: string
}

/**
 * The commands that install the published packages.
 *
 * Without a catalog the page keeps the update command and drops the package
 * list, because there is no trustworthy list to show.
 */
export function installCommands(
  packages: readonly PackageSummary[] | null,
  labels: ArchitectureLabels,
): readonly CommandBlock[] {
  if (packages === null || packages.length === 0) {
    return [{ command: 'sudo apt update' }]
  }

  const shared = packages.filter(
    (pkg) => pkg.architectures.includes('amd64') && pkg.architectures.includes('arm64'),
  )
  const amd64Only = packages.filter(
    (pkg) => pkg.architectures.includes('amd64') && !pkg.architectures.includes('arm64'),
  )
  const arm64Only = packages.filter(
    (pkg) => pkg.architectures.includes('arm64') && !pkg.architectures.includes('amd64'),
  )

  const blocks: CommandBlock[] = []
  blocks.push({
    command:
      shared.length > 0
        ? `sudo apt update && sudo apt install ${names(shared)}`
        : 'sudo apt update',
  })
  if (amd64Only.length > 0) {
    blocks.push({ label: labels.amd64, command: `sudo apt install ${names(amd64Only)}` })
  }
  if (arm64Only.length > 0) {
    blocks.push({ label: labels.arm64, command: `sudo apt install ${names(arm64Only)}` })
  }
  return blocks
}

/** The commands that configure device credentials. */
export function authCommands(origin: string): readonly string[] {
  return [
    `sudo install -d -m0755 /etc/apt/auth.conf.d
sudo touch /etc/apt/auth.conf.d/apt-at-home.conf
sudo chmod 600 /etc/apt/auth.conf.d/apt-at-home.conf
sudoedit /etc/apt/auth.conf.d/apt-at-home.conf`,
    `machine ${origin} login DEVICE password TOKEN`,
    `sudo curl --fail --show-error --user DEVICE --output /etc/apt/keyrings/apt-at-home.asc ${origin}/key.asc`,
  ]
}

/** Joins validated package names into one shell argument list. */
function names(packages: readonly PackageSummary[]): string {
  return packages.map((pkg) => pkg.name).join(' ')
}
