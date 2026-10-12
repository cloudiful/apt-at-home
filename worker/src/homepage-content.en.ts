/**
 * English prose for the public root landing page.
 *
 * Command and configuration blocks are not repeated here: they come from
 * `homepage-commands.ts` and are shared with every other locale.
 */
import type { HomepageCopy } from './homepage-content.js';

export const homepageCopy: HomepageCopy = {
  lede: 'A public, test-mode APT repository for Debian 13, publishing <strong>bottom</strong>, <strong>git-delta</strong>, <strong>kandev</strong>, <strong>neovim</strong>, <strong>rustfs-cli</strong>, <strong>sccache</strong>, <strong>starship</strong>, <strong>uv</strong>, <strong>zellij</strong>, and <strong>zoxide</strong> packages for amd64 and arm64.',
  languageLabel: 'Language',
  prerequisites: {
    heading: 'Before you start',
    body: 'This guide needs Debian 13 with <code>curl</code> already installed, and configures a client only. The repository must have completed its first publish, or there is nothing to install yet. Downloads currently require no credentials, so treat the hostname as a public resource.',
  },
  stepsHeading: 'Installation steps',
  steps: {
    key: {
      title: 'Install the signing key',
      intro: 'Download the armored public key into the keyring apt trusts for this repository:',
      fingerprintSummary: 'Verify the key fingerprint (optional)',
      fingerprintNote: 'The download above trusts this server for the key. Compare the full primary fingerprint with a value you received through a separate, independently trusted channel; never trust a fingerprint printed by this page or the gateway.',
    },
    sources: {
      title: 'Add the repository',
      intro: 'Write the Deb822 source entry, signed by the key installed above:',
      architectureNote: 'The architecture is selected automatically from the local dpkg; no foreign architecture has to be forced.',
    },
    install: {
      title: 'Update and install',
      intro: 'Install the published packages (<code>bottom</code> provides <code>btm</code>, <code>git-delta</code> provides <code>delta</code>, <code>rustfs-cli</code> provides <code>rc</code>, and <code>uv</code> provides <code>uv</code> and <code>uvx</code>). The <code>kandev</code> package installs the standard server bundle; run <code>kandev</code> to start it. Zoxide Bash integration is optional: add <code>eval "$(zoxide init bash)"</code> to your shell configuration if wanted; installation does not edit it.',
    },
    auth: {
      summary: 'Device credentials (only when authentication is enabled)',
      intro: 'This repository currently serves downloads anonymously. If the administrator re-enables the credential gate, write a credential file and fetch the key with your device credentials instead; the source entry does not change.',
      fileLabel: 'File contents (<code>machine</code> uses the full source address):',
      curlNote: 'With the gate enabled, the key fetch uses your device name and prompts for the password instead of the anonymous command above:',
    },
  },
  footer: {
    before: 'See the ',
    linkText: 'README and docs/setup.md in the public repository',
    after: ' for operator detail. Packages, indexes, and the signing key are served without credentials while the repository stays in public test mode.',
  },
};
