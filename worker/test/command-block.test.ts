import { afterEach, describe, expect, it, vi } from 'vitest';
import { createCopyFeedback } from '../../web/src/components/command-block';

afterEach(() => vi.useRealTimers());

describe('copy feedback lifetime', () => {
  it.each(['copied', 'failed'] as const)('restarts repeated %s feedback', (result) => {
    vi.useFakeTimers();
    const update = vi.fn();
    const feedback = createCopyFeedback(update);
    feedback.show(result);
    vi.advanceTimersByTime(1500);
    feedback.show(result);
    vi.advanceTimersByTime(1000);
    expect(update.mock.calls).toEqual([[result], [result]]);
    vi.advanceTimersByTime(999);
    expect(update).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(1);
    expect(update).toHaveBeenLastCalledWith('idle');
    feedback.dispose();
  });

  it('restarts alternating results without affecting another block', () => {
    vi.useFakeTimers();
    const first = vi.fn();
    const second = vi.fn();
    const a = createCopyFeedback(first);
    const b = createCopyFeedback(second);
    a.show('copied');
    b.show('failed');
    vi.advanceTimersByTime(1500);
    a.show('failed');
    vi.advanceTimersByTime(1000);
    expect(first.mock.calls).toEqual([['copied'], ['failed']]);
    expect(second).toHaveBeenLastCalledWith('idle');
    vi.advanceTimersByTime(1000);
    expect(first).toHaveBeenLastCalledWith('idle');
    a.dispose();
    b.dispose();
  });

  it('cancels pending reset and ignores completed operations after disposal', () => {
    vi.useFakeTimers();
    const update = vi.fn();
    const feedback = createCopyFeedback(update);
    feedback.show('copied');
    feedback.dispose();
    feedback.show('failed');
    vi.runAllTimers();
    expect(update.mock.calls).toEqual([['copied']]);
  });
});
