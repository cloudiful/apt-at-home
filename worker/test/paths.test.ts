import { describe, expect, it } from 'vitest';

import { classifyPath, parsePointer } from '../src/paths.js';

const DIGEST = 'a'.repeat(64);

function keyFor(path: string): string | null {
  return classifyPath(path)?.key ?? null;
}

describe('classifyPath', () => {
  it('maps a package file to its write-once pool key', () => {
    expect(classifyPath('/pool/zellij_0.45.1-1_amd64.deb')).toEqual({
      key: 'pool/zellij_0.45.1-1_amd64.deb',
      immutable: true,
      releaseRelative: false,
    });
  });

  it('maps the signing key to its own key', () => {
    expect(keyFor('/key.asc')).toBe('key.asc');
  });

  it('maps distribution metadata relative to the published release', () => {
    expect(classifyPath('/dists/stable/Release')).toEqual({
      key: 'dists/stable/Release',
      immutable: false,
      releaseRelative: true,
    });
    expect(keyFor('/dists/stable/InRelease')).toBe('dists/stable/InRelease');
    expect(keyFor('/dists/stable/Release.gpg')).toBe('dists/stable/Release.gpg');
    expect(keyFor('/dists/stable/main/binary-arm64/Packages.gz')).toBe(
      'dists/stable/main/binary-arm64/Packages.gz',
    );
  });

  it('maps a by-hash entry to the shared content-addressed index', () => {
    expect(classifyPath(`/dists/stable/main/binary-amd64/by-hash/SHA256/${DIGEST}`)).toEqual({
      key: `indices/amd64/SHA256/${DIGEST}`,
      immutable: true,
      releaseRelative: false,
    });
  });

  it.each([
    ['/', 'root'],
    ['//pool/zellij_0.45.1-1_amd64.deb', 'double leading slash'],
    ['/pool/', 'empty segment'],
    ['/pool//zellij_0.45.1-1_amd64.deb', 'empty inner segment'],
    ['/pool/zellij_0.45.1-1_amd64.deb/', 'trailing slash'],
    ['/pool/%2e%2e%2fkey.asc', 'encoded traversal'],
    ['/pool/%2E%2E', 'encoded dot segment'],
    ['/pool/..%2Fcurrent.json', 'partially encoded traversal'],
    ['/pool%2Fzellij_0.45.1-1_amd64.deb', 'encoded separator'],
    ['/pool%5czellij.deb', 'encoded backslash'],
    ['/pool/%zz', 'invalid percent encoding'],
    ['/pool/%', 'truncated percent encoding'],
    ['/pool/.', 'dot segment'],
    ['/pool/..', 'parent segment'],
    ['/pool/current.json', 'pool file that is not a package'],
    ['/pool/zellij.deb.bak', 'pool file with a foreign extension'],
    ['/key.asc/x', 'extra segment under the key'],
    ['/dists', 'suite root'],
    ['/dists/testing/Release', 'unsupported suite'],
    ['/dists/stable', 'suite without a file'],
    ['/dists/stable/contrib/binary-amd64/Packages', 'unsupported component'],
    ['/dists/stable/main/binary-i386/Packages', 'unsupported architecture'],
    ['/dists/stable/main/binary-amd64/Packages.xz', 'unsupported compression'],
    ['/dists/stable/main/binary-amd64/debian-installer/Packages', 'unknown metadata directory'],
    [`/dists/stable/main/binary-amd64/by-hash/SHA512/${DIGEST}`, 'unsupported hash algorithm'],
    [`/dists/stable/main/binary-amd64/by-hash/MD5Sum/${DIGEST}`, 'unsupported hash name'],
    ['/dists/stable/main/binary-amd64/by-hash/SHA256/zz', 'digest that is not hex'],
    [`/dists/stable/main/binary-amd64/by-hash/SHA256/${'A'.repeat(64)}`, 'uppercase digest'],
    ['/current.json', 'pointer is never served'],
    ['/releases/20260102-030405/dists/stable/Release', 'release path is not servable'],
    ['/indices/amd64/SHA256/abc', 'index path is not servable'],
    ['/../key.asc', 'leading traversal'],
    ['pool/x.deb', 'path that is not absolute'],
  ])('rejects %s (%s)', (path) => {
    expect(classifyPath(path)).toBeNull();
  });
});

describe('parsePointer', () => {
  it('accepts a well formed release id', () => {
    expect(parsePointer('{"release":"20260102-030405"}')).toEqual({
      ok: true,
      releaseId: '20260102-030405',
    });
  });

  it.each([
    ['not json', 'not json at all'],
    ['array', '[]'],
    ['null', 'null'],
    ['missing release', '{"published_at":"2026-01-02"}'],
    ['non-string release', '{"release":7}'],
    ['empty release', '{"release":""}'],
    ['traversal release', '{"release":"../.."}'],
    ['separator in release', '{"release":"a/b"}'],
    ['leading dot release', '{"release":".hidden"}'],
    ['overlong release', `{"release":"${'a'.repeat(129)}"}`],
  ])('reports an invalid pointer: %s', (_label, body) => {
    expect(parsePointer(body)).toEqual({ ok: false, reason: 'invalid' });
  });
});