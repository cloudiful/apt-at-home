/**
 * English prose for the public root landing page.
 *
 * Command and configuration blocks are not repeated here: they come from
 * `homepage-commands.ts` and are shared with every other locale. The package
 * roster is not written here either: it comes from the published catalog.
 */
import type { HomepageCopy } from './homepage-content.js';

export const homepageCopy: HomepageCopy = {
  languageLabel: 'Language',
  catalog: {
    heading: 'Published packages',
    caption: 'Published packages',
    packageColumn: 'Package',
    versionColumn: 'Upstream version',
    architectureColumn: 'Architecture',
    unavailable: 'The published package list is unavailable right now.',
  },
  stepsHeading: 'Installation steps',
  steps: {
    key: {
      title: 'Install the signing key',
      fingerprintSummary: 'Verify the key fingerprint (optional)',
      fingerprintNote: 'The download above trusts this server for the key. Compare the full primary fingerprint with a value you received through a separate, independently trusted channel; never trust a fingerprint printed by this page or the gateway.',
    },
    sources: {
      title: 'Add the repository',
    },
    install: {
      title: 'Update and install',
      amd64Intro: 'Only on amd64:',
      arm64Intro: 'Only on arm64:',
    },
    auth: {
      summary: 'Device credentials (only when authentication is enabled)',
      intro: 'This repository currently serves downloads anonymously. If the administrator re-enables the credential gate, write a credential file and fetch the key with your device credentials instead; the source entry does not change.',
      fileLabel: 'File contents (<code>machine</code> uses the full source address):',
      curlNote: 'With the gate enabled, the key fetch uses your device name and prompts for the password instead of the anonymous command above:',
    },
  },
  footer: {
    linkText: 'Documentation',
  },
};
