import { describe, expect, it } from 'vitest';

import { challengeHeaders, isAuthorized, parseCredentialConfig } from '../src/auth.js';
import { credentialSecret, sha256Hex } from './fakes.js';

const NAME = 'laptop';
const TOKEN = 'a-long-per-device-token';

async function credentials(entries: Record<string, string> = { [NAME]: TOKEN }) {
  const config = parseCredentialConfig(await credentialSecret(entries));
  if (!config.ok) throw new Error('fixture secret should be valid');
  return config.credentials;
}

function header(value: string): string {
  return `Basic ${value}`;
}

describe('parseCredentialConfig', () => {
  it('accepts a well formed secret and normalises digest case', async () => {
    const digest = (await sha256Hex(TOKEN)).toUpperCase();
    const config = parseCredentialConfig(JSON.stringify({ [NAME]: digest }));
    expect(config.ok).toBe(true);
    if (!config.ok) return;
    expect(config.credentials.get(NAME)).toBe(await sha256Hex(TOKEN));
  });

  it.each([
    ['missing', undefined],
    ['empty', '   '],
    ['not json', '{'],
    ['array', '[]'],
    ['null', 'null'],
    ['scalar', '"laptop"'],
    ['empty object', '{}'],
    ['non-string digest', JSON.stringify({ [NAME]: 12345 })],
    ['short digest', JSON.stringify({ [NAME]: 'abcd' })],
    ['non-hex digest', JSON.stringify({ [NAME]: 'z'.repeat(64) })],
    ['invalid name', JSON.stringify({ 'bad name': 'a'.repeat(64) })],
    ['digest of the wrong length', JSON.stringify({ [NAME]: `${'a'.repeat(63)}` })],
  ])('rejects a secret that is %s', (_label, raw) => {
    expect(parseCredentialConfig(raw).ok).toBe(false);
  });

  it('does not resolve a client name through the prototype chain', async () => {
    const map = await credentials();
    const digest = await sha256Hex(TOKEN);
    expect(await isAuthorized(header(btoa(`constructor:${TOKEN}`)), map)).toBe(false);
    expect(await isAuthorized(header(btoa(`constructor:${digest}`)), map)).toBe(false);
  });
});

describe('isAuthorized', () => {
  it('accepts the exact token of a known client', async () => {
    expect(await isAuthorized(header(btoa(`${NAME}:${TOKEN}`)), await credentials())).toBe(true);
  });

  it('rejects a wrong token for a known client', async () => {
    expect(await isAuthorized(header(btoa(`${NAME}:wrong`)), await credentials())).toBe(false);
  });

  it('rejects an unknown client name', async () => {
    expect(await isAuthorized(header(btoa('phone:whatever')), await credentials())).toBe(false);
  });

  it.each([
    ['no header', null],
    ['empty header', ''],
    ['scheme only', 'Basic'],
    ['wrong scheme', `Bearer ${btoa(`${NAME}:${TOKEN}`)}`],
    ['not base64', 'Basic ****'],
    ['no separator', header(btoa(NAME))],
    ['empty token', header(btoa(`${NAME}:`))],
    ['token with whitespace', header(btoa(`${NAME}:two words`))],
    ['token with a control byte', header(btoa(`${NAME}:tok${String.fromCharCode(0)}en`))],
    ['invalid name', header(btoa('bad name:whatever'))],
  ])('rejects %s', async (_label, value) => {
    expect(await isAuthorized(value, await credentials())).toBe(false);
  });

  it('accepts the scheme in any case, as RFC 7617 allows', async () => {
    const map = await credentials();
    expect(await isAuthorized(`basic ${btoa(`${NAME}:${TOKEN}`)}`, map)).toBe(true);
  });

  it('rejects a header longer than the accepted bound', async () => {
    const map = await credentials();
    const oversized = `Basic ${btoa(`${NAME}:${'x'.repeat(5000)}`)}`;
    expect(oversized.length).toBeGreaterThan(4096);
    expect(await isAuthorized(oversized, map)).toBe(false);
  });

  it('rejects a decoded credential longer than the accepted bound', async () => {
    const map = await credentials();
    const long = `Basic ${btoa(`${NAME}:${'x'.repeat(600)}`)}`;
    expect(await isAuthorized(long, map)).toBe(false);
  });

  it('never accepts a digest of the token in place of the token', async () => {
    const map = await credentials();
    const digest = await sha256Hex(TOKEN);
    expect(await isAuthorized(header(btoa(`${NAME}:${digest}`)), map)).toBe(false);
  });
});

describe('challengeHeaders', () => {
  it('advertises Basic auth and forbids storage of the rejection', () => {
    const headers = challengeHeaders();
    expect(headers['WWW-Authenticate']).toContain('Basic realm=');
    expect(headers['Cache-Control']).toBe('no-store');
  });
});