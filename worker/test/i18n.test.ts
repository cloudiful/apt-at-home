import { describe, expect, it } from 'vitest';

import {
  DEFAULT_LOCALE,
  documentLanguage,
  isLocale,
  negotiateLocale,
  parseLocaleOverride,
  resolveLocale,
  SUPPORTED_LOCALES,
} from '../src/i18n.js';

describe('supported locales', () => {
  it('exposes exactly English and Chinese with English as the fallback', () => {
    expect([...SUPPORTED_LOCALES]).toEqual(['en', 'zh']);
    expect(DEFAULT_LOCALE).toBe('en');
    expect(isLocale('en')).toBe(true);
    expect(isLocale('zh')).toBe(true);
    expect(isLocale('fr')).toBe(false);
  });

  it('maps each locale to its document and content language', () => {
    expect(documentLanguage('en')).toBe('en');
    expect(documentLanguage('zh')).toBe('zh-CN');
  });
});

describe('negotiateLocale', () => {
  it.each<[string, string | null, 'en' | 'zh']>([
    ['missing header falls back to English', null, 'en'],
    ['empty header falls back to English', '', 'en'],
    ['unsupported languages fall back to English', 'fr-FR,de;q=0.9', 'en'],
    ['a bare supported tag wins', 'zh', 'zh'],
    ['region subtags match on the primary tag', 'en-US', 'en'],
    ['script subtags match on the primary tag', 'zh-Hant', 'zh'],
    ['the highest quality wins', 'en;q=0.8,zh;q=0.9', 'zh'],
    ['an implicit quality of 1 outranks an explicit lower one', 'en,zh;q=0.9', 'en'],
    ['the first of equal qualities wins', 'zh,en', 'zh'],
    ['the first of equal qualities wins in the other order', 'en,zh', 'en'],
    ['whitespace and casing are ignored', '  ZH ; q=0.9 , EN;q=0.8 ', 'zh'],
    ['q=1.0 is valid', 'zh;q=1.0', 'zh'],
    ['q=0 excludes the entry', 'zh;q=0,en;q=0.5', 'en'],
    ['q=0 with no fallback still yields English', 'zh;q=0', 'en'],
    ['a wildcard is skipped', '*;q=0.9,zh;q=0.5', 'zh'],
    ['a malformed q ignores only its own entry', 'zh;q=abc,en;q=0.5', 'en'],
    ['an out-of-range q ignores its own entry', 'zh;q=2,en;q=0.5', 'en'],
    ['an unrecognized parameter is ignored', 'zh;level=1', 'zh'],
    ['a valueless parameter invalidates its entry', 'zh;broken', 'en'],
    ['zero is only accepted as an in-range quality', 'en;q=0.0,zh;q=0.1', 'zh'],
  ])('%s', (_name, header, expected) => {
    expect(negotiateLocale(header)).toBe(expected);
  });

  it('bounds the parsed header length so a padded supported tag is ignored', () => {
    const padded = 'x'.repeat(5000) + ',zh';
    expect(negotiateLocale(padded)).toBe('en');
    const early = 'en;q=0.5,' + 'x'.repeat(5000);
    expect(negotiateLocale(early)).toBe('en');
  });
});

describe('parseLocaleOverride', () => {
  it.each<[string | null, 'en' | 'zh' | null]>([
    [null, null],
    ['', null],
    ['en', 'en'],
    ['zh', 'zh'],
    ['EN', 'en'],
    ['  Zh  ', 'zh'],
    ['en-US', null],
    ['fr', null],
    ['<script>', null],
  ])('parses %s', (raw, expected) => {
    expect(parseLocaleOverride(raw)).toBe(expected);
  });
});

describe('resolveLocale', () => {
  it('lets a supported override win over the header', () => {
    expect(resolveLocale('zh', 'en-US')).toBe('zh');
    expect(resolveLocale('en', 'zh-CN')).toBe('en');
  });

  it('falls back to the header when the override is absent or unsupported', () => {
    expect(resolveLocale(null, 'zh-CN')).toBe('zh');
    expect(resolveLocale('fr', 'zh-CN')).toBe('zh');
    expect(resolveLocale('', 'en')).toBe('en');
  });
});
