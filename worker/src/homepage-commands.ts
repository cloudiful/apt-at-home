/**
 * Shared markup builders for the landing page's installation steps.
 *
 * The four steps carry command and configuration blocks that are byte-identical
 * across locales apart from the request origin, so those blocks live here once
 * and the per-locale content modules supply only prose.
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

/** Localized prose for the credential-file step; the command block is shared. */
export interface PrepareStepCopy {
  readonly title: string;
  readonly intro: string;
  readonly entryLabel: string;
}

/** Localized prose for the signing-key step. */
export interface KeyStepCopy {
  readonly title: string;
  readonly fetchIntro: string;
  readonly verifyIntro: string;
  readonly installIntro: string;
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
}

export interface InstallStepsCopy {
  readonly prepare: PrepareStepCopy;
  readonly key: KeyStepCopy;
  readonly sources: SourcesStepCopy;
  readonly install: InstallStepCopy;
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
        <h3>${copy.prepare.title}</h3>
        <p>${copy.prepare.intro}</p>
        <pre><code>sudo mkdir -p /etc/apt/keyrings /etc/apt/auth.conf.d
sudo touch /etc/apt/auth.conf.d/apt-at-home.conf
sudo chmod 600 /etc/apt/auth.conf.d/apt-at-home.conf
sudoedit /etc/apt/auth.conf.d/apt-at-home.conf</code></pre>
        <p>${copy.prepare.entryLabel}</p>
        <pre><code>machine ${o} login DEVICE password TOKEN</code></pre>
      </li>
      <li>
        <h3>${copy.key.title}</h3>
        <p>${copy.key.fetchIntro}</p>
        <pre><code>curl --fail --show-error --user DEVICE --output key.asc ${o}/key.asc
gpg --show-keys --with-fingerprint key.asc</code></pre>
        <p>${copy.key.verifyIntro}</p>
        <pre><code>gpg --dearmor --output apt-at-home.gpg key.asc
sudo install -m0644 apt-at-home.gpg /etc/apt/keyrings/apt-at-home.gpg</code></pre>
        <p>${copy.key.installIntro}</p>
      </li>
      <li>
        <h3>${copy.sources.title}</h3>
        <p>${copy.sources.intro}</p>
        <pre><code>Types: deb
URIs: ${o}
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/apt-at-home.gpg</code></pre>
        <p>${copy.sources.architectureNote}</p>
      </li>
      <li>
        <h3>${copy.install.title}</h3>
        <pre><code>sudo apt update
sudo apt install zellij starship</code></pre>
      </li>`;
}
