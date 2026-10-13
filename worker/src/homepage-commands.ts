/**
 * Shared markup builders for the landing page's installation steps.
 *
 * The three steps carry command and configuration blocks that are
 * byte-identical across locales apart from the request origin, so those blocks
 * live here once and the per-locale content modules supply only prose. The
 * origin is escaped here before it reaches any command or attribute, so a
 * crafted hostname can never inject markup into the page.
 */

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
  readonly intro: string;
  readonly fingerprintSummary: string;
  readonly fingerprintNote: string;
}

/** Localized prose for the sources step. */
export interface SourcesStepCopy {
  readonly title: string;
  readonly intro: string;
  readonly architectureNote: string;
}

/** Localized prose for the update-and-install step. */
export interface InstallStepCopy {
  readonly title: string;
  readonly intro: string;
  readonly amd64Intro: string;
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
 * locale modules never interpolate request data themselves.
 */
export function renderInstallSteps(origin: string, copy: InstallStepsCopy): string {
  const o = escapeHtml(origin);
  return `      <li>
        <h3>${copy.key.title}</h3>
        <p>${copy.key.intro}</p>
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
        <p>${copy.sources.intro}</p>
        <pre><code>sudo tee /etc/apt/sources.list.d/apt-at-home.sources &gt;/dev/null &lt;&lt;'EOF'
Types: deb
URIs: ${o}
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/apt-at-home.asc
EOF</code></pre>
        <p>${copy.sources.architectureNote}</p>
      </li>
      <li>
        <h3>${copy.install.title}</h3>
        <p>${copy.install.intro}</p>
        <pre><code>sudo apt update &amp;&amp; sudo apt install bottom git-delta kandev neovim rustfs-cli sccache starship uv zellij zoxide</code></pre>
        <p>${copy.install.amd64Intro}</p>
        <pre><code>sudo apt install cargo-tauri</code></pre>
      </li>`;
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
