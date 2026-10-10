/**
 * English prose for the public root landing page.
 *
 * Command and configuration blocks are not repeated here: they come from
 * `homepage-commands.ts` and are shared with every other locale.
 */
import type { HomepageCopy } from './homepage-content.js';

export const homepageCopy: HomepageCopy = {
  lede: 'A private, per-device authenticated APT repository for Debian 13, publishing <strong>zellij</strong> and <strong>starship</strong> packages for amd64 and arm64.',
  languageLabel: 'Language',
  prerequisites: {
    heading: 'Before you start',
    body: 'Ask the administrator for this device’s <strong>username and token</strong> and for the <strong>full trusted primary key fingerprint</strong>. The repository must have completed its first publish, or the commands below have nothing to install. This page only explains how to configure a client; it does not mean the repository is ready.',
  },
  stepsHeading: 'Installation steps',
  steps: {
    prepare: {
      title: 'Prepare the credential file',
      intro: 'Create the keyring and authentication directories, then use <code>sudo touch</code> followed by <code>sudo chmod 600</code>, and write the file with <code>sudoedit</code>. The values below are placeholders: <code>DEVICE</code> and <code>TOKEN</code> are not real. Never put a real token in an executable script, a command-line argument, or a URL; the server offers no credential-creation script.',
      entryLabel: 'File contents (<code>machine</code> uses the full source address):',
    },
    key: {
      title: 'Fetch and verify the signing key',
      fetchIntro: 'Fetch the public key with <code>curl</code> (curl prompts for the password; <code>--location</code> is not used):',
      verifyIntro: 'Before importing, compare <strong>the full primary key fingerprint</strong> received through a separate channel character by character; do not trust a fingerprint printed here or by the gateway. Once it matches, convert to a binary keyring and install it:',
      installIntro: 'apt uses the binary <code>.gpg</code> keyring, not the armored <code>.asc</code> file, and apt’s <code>auth.conf</code> is not a curl netrc; their matching syntax differs.',
    },
    sources: {
      title: 'Add the repository',
      intro: 'Write the Deb822 configuration with <code>sudoedit /etc/apt/sources.list.d/apt-at-home.sources</code>:',
      architectureNote: 'The architecture is selected automatically from the local dpkg; no foreign architecture has to be forced.',
    },
    install: {
      title: 'Update and install',
    },
  },
  footer: {
    before: 'See the ',
    linkText: 'README and docs/setup.md in the public repository',
    after: ' for more detail. Packages, indexes, and the signing key stay private; this page is the only public one.',
  },
};
