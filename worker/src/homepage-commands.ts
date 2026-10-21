/**
 * Shared markup builders for the landing page's installation steps.
 *
 * The three steps carry command and configuration blocks that are
 * byte-identical across locales apart from the request origin, so those blocks
 * live here once and the per-locale content modules supply only prose. The
 * origin is escaped here before it reaches any command or attribute, so a
 * crafted hostname can never inject markup into the page.
 *
 * The install commands are derived from the published catalog rather than
 * written out: the package names come from the manifest the publisher already
 * validated, and the reader only ever sees names the shell accepts as plain
 * arguments.
 */
import type { PackageSummary } from './packages.js';

/** Escapes a value for interpolation into element text or a quoted attribute. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Localized prose for the signing-key step; the command block is shared. */
export interface KeyStepCopy {
  readonly title: string;
  readonly fingerprintSummary: string;
  readonly fingerprintNote: string;
}

/** Localized prose for the sources step. */
export interface SourcesStepCopy {
  readonly title: string;
}

/** Localized prose for the update-and-install step. */
export interface InstallStepCopy {
  readonly title: string;
  readonly amd64Intro: string;
  readonly arm64Intro: string;
}

/** Localized prose for the collapsed device-credential details. */
export interface AuthDetailsCopy {
  readonly summary: string;
  readonly intro: string;
  readonly fileLabel: string;
  readonly curlNote: string;
}

export interface InstallStepsCopy {
  readonly key: KeyStepCopy;
  readonly sources: SourcesStepCopy;
  readonly install: InstallStepCopy;
  readonly auth: AuthDetailsCopy;
}

/**
 * Renders the ordered installation steps for one request origin.
 *
 * The origin is HTML-escaped here and reused in every command block, so the
 * locale modules never interpolate request data themselves. Each step is a
 * title and its commands: the commands say what to run, so no step repeats
 * itself in an introductory paragraph.
 */
export function renderInstallSteps(
  origin: string,
  copy: InstallStepsCopy,
  packages?: readonly PackageSummary[],
): string {
  const o = escapeHtml(origin);
  return `      <li>
        <h3>${copy.key.title}</h3>
        <pre><code>sudo install -d -m0755 /etc/apt/keyrings
sudo curl --fail --show-error --output /etc/apt/keyrings/apt-at-home.asc ${o}/key.asc
sudo chmod 0644 /etc/apt/keyrings/apt-at-home.asc</code></pre>
        <details class="more">
          <summary>${copy.key.fingerprintSummary}</summary>
          <p>${copy.key.fingerprintNote}</p>
          <pre><code>gpg --show-keys /etc/apt/keyrings/apt-at-home.asc</code></pre>
        </details>
      </li>
      <li>
        <h3>${copy.sources.title}</h3>
        <pre><code>sudo tee /etc/apt/sources.list.d/apt-at-home.sources &gt;/dev/null &lt;&lt;'EOF'
Types: deb
URIs: ${o}
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/apt-at-home.asc
EOF</code></pre>
      </li>
      <li>
        <h3>${copy.install.title}</h3>
${renderInstallCommands(copy.install, packages)}
      </li>`;
}

/**
 * Renders the install commands for the published catalog.
 *
 * Packages that exist for both architectures are installed with one command;
 * packages that exist for one architecture only get their own command, so the
 * page never suggests installing a package the architecture does not carry.
 * Without a catalog the page keeps the update command and drops the package
 * list, because there is no trustworthy list to show.
 */
function renderInstallCommands(
  copy: InstallStepCopy,
  packages: readonly PackageSummary[] | undefined,
): string {
  if (packages === undefined || packages.length === 0) {
    return `        <pre><code>sudo apt update</code></pre>`;
  }

  const shared = packages.filter(
    (pkg) => pkg.architectures.includes('amd64') && pkg.architectures.includes('arm64'),
  );
  const amd64Only = packages.filter(
    (pkg) => pkg.architectures.includes('amd64') && !pkg.architectures.includes('arm64'),
  );
  const arm64Only = packages.filter(
    (pkg) => pkg.architectures.includes('arm64') && !pkg.architectures.includes('amd64'),
  );

  const blocks: string[] = [];
  blocks.push(
    shared.length > 0
      ? `        <pre><code>sudo apt update &amp;&amp; sudo apt install ${names(shared)}</code></pre>`
      : `        <pre><code>sudo apt update</code></pre>`,
  );
  if (amd64Only.length > 0) {
    blocks.push(`        <p>${copy.amd64Intro}</p>`);
    blocks.push(`        <pre><code>sudo apt install ${names(amd64Only)}</code></pre>`);
  }
  if (arm64Only.length > 0) {
    blocks.push(`        <p>${copy.arm64Intro}</p>`);
    blocks.push(`        <pre><code>sudo apt install ${names(arm64Only)}</code></pre>`);
  }
  return blocks.join('\n');
}

/** Joins validated package names into one shell argument list. */
function names(packages: readonly PackageSummary[]): string {
  return packages.map((pkg) => pkg.name).join(' ');
}

/**
 * Renders the collapsed device-credential guidance.
 *
 * The repository serves downloads anonymously while the credential gate stays
 * disabled. This block is collapsed by default and only documents the extra
 * steps that become necessary if the operator re-enables authentication; it
 * never carries a real token, only placeholders.
 */
export function renderAuthDetails(origin: string, copy: AuthDetailsCopy): string {
  const o = escapeHtml(origin);
  return `    <details class="more">
      <summary>${copy.summary}</summary>
      <p>${copy.intro}</p>
      <pre><code>sudo install -d -m0755 /etc/apt/auth.conf.d
sudo touch /etc/apt/auth.conf.d/apt-at-home.conf
sudo chmod 600 /etc/apt/auth.conf.d/apt-at-home.conf
sudoedit /etc/apt/auth.conf.d/apt-at-home.conf</code></pre>
      <p>${copy.fileLabel}</p>
      <pre><code>machine ${o} login DEVICE password TOKEN</code></pre>
      <p>${copy.curlNote}</p>
      <pre><code>sudo curl --fail --show-error --user DEVICE --output /etc/apt/keyrings/apt-at-home.asc ${o}/key.asc</code></pre>
    </details>`;
}
