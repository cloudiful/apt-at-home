/**
 * Pure relative-date helper: UTC day buckets from fixed instants.
 *
 * Every case passes an explicit request clock, so the helper never reads the
 * live clock and the server markup stays coherent with hydration.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';

import { copyFor } from '../../web/src/lib/locales.js';
import { exactReleaseDate, relativeReleaseLabel } from '../../web/src/lib/release-date.js';

describe('relative release labels', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows today for the same UTC calendar day in both locales', () => {
    expect(
      relativeReleaseLabel('2026-10-12T00:00:00Z', '2026-10-12T23:59:59Z', copyFor('en').catalog),
    ).toBe('Today');
    expect(
      relativeReleaseLabel('2026-10-12T23:59:59Z', '2026-10-12T00:00:01Z', copyFor('zh').catalog),
    ).toBe('今天');
  });

  it('shows today for a same-day future instant', () => {
    expect(
      relativeReleaseLabel('2026-10-12T15:00:00Z', '2026-10-12T10:00:00Z', copyFor('en').catalog),
    ).toBe('Today');
  });

  it('splits UTC midnight into today and yesterday', () => {
    expect(
      relativeReleaseLabel('2026-10-11T23:59:59Z', '2026-10-12T00:00:00Z', copyFor('en').catalog),
    ).toBe('Yesterday');
    expect(
      relativeReleaseLabel('2026-10-12T00:00:00Z', '2026-10-12T00:00:00Z', copyFor('en').catalog),
    ).toBe('Today');
    expect(
      relativeReleaseLabel('2026-10-11T23:59:59Z', '2026-10-12T00:00:00Z', copyFor('zh').catalog),
    ).toBe('昨天');
  });

  it('counts N days ago across the example and large ages', () => {
    const en = copyFor('en').catalog;
    const zh = copyFor('zh').catalog;
    expect(relativeReleaseLabel('2026-10-09T15:43:25Z', '2026-10-12T00:00:00Z', en)).toBe(
      '3 days ago',
    );
    expect(relativeReleaseLabel('2026-10-09T15:43:25Z', '2026-10-12T00:00:00Z', zh)).toBe(
      '3 天前',
    );
    expect(relativeReleaseLabel('2025-01-01T00:00:00Z', '2026-10-12T00:00:00Z', en)).toBe(
      '649 days ago',
    );
    expect(relativeReleaseLabel('2020-01-01T00:00:00Z', '2026-10-12T12:00:00Z', zh)).toBe(
      '2476 天前',
    );
  });

  it('returns no relative for future UTC calendar days', () => {
    const en = copyFor('en').catalog;
    expect(relativeReleaseLabel('2026-10-13T00:00:00Z', '2026-10-12T23:59:59Z', en)).toBeNull();
    expect(relativeReleaseLabel('2027-01-01T00:00:00Z', '2026-10-12T00:00:00Z', en)).toBeNull();
  });

  it('returns no relative for unknown or unparsable values', () => {
    const en = copyFor('en').catalog;
    expect(relativeReleaseLabel(undefined, '2026-10-12T00:00:00Z', en)).toBeNull();
    expect(relativeReleaseLabel('not-a-date', '2026-10-12T00:00:00Z', en)).toBeNull();
    expect(relativeReleaseLabel('', '2026-10-12T00:00:00Z', en)).toBeNull();
    expect(relativeReleaseLabel('2026-10-12T00:00:00Z', 'not-a-date', en)).toBeNull();
    expect(relativeReleaseLabel('2026-02-31T00:00:00Z', '2026-10-12T00:00:00Z', en)).toBeNull();
  });

  it('keeps the exact UTC day locale-independent', () => {
    expect(exactReleaseDate('2026-10-09T15:43:25Z')).toBe('2026-10-09');
    expect(exactReleaseDate('2026-10-12T00:00:00Z')).toBe('2026-10-12');
  });

  it('counts early years 0000 and 0099 without Date.UTC remapping', () => {
    const en = copyFor('en').catalog;
    expect(relativeReleaseLabel('0099-12-31T00:00:00Z', '2026-10-24T00:00:00Z', en)).toBe(
      '703754 days ago',
    );
    expect(relativeReleaseLabel('0000-01-01T00:00:00Z', '2026-10-24T00:00:00Z', en)).toBe(
      '740278 days ago',
    );
  });

  it('ignores the live system clock once the request clock is fixed', () => {
    const en = copyFor('en').catalog;
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2030-05-01T12:00:00Z'));
    expect(relativeReleaseLabel('2026-10-09T15:43:25Z', '2026-10-12T00:00:00Z', en)).toBe(
      '3 days ago',
    );
    vi.setSystemTime(new Date('2020-01-01T00:00:00Z'));
    expect(relativeReleaseLabel('2026-10-09T15:43:25Z', '2026-10-12T00:00:00Z', en)).toBe(
      '3 days ago',
    );
  });
});
