import { describe, expect, it } from 'vitest';

import wranglerSource from '../wrangler.jsonc?raw';

interface WranglerConfig {
  readonly name: string;
  readonly main: string;
  readonly compatibility_date: string;
  readonly account_id?: string;
  readonly workers_dev?: boolean;
  readonly preview_urls?: boolean;
  readonly keep_vars?: boolean;
  readonly routes?: unknown;
  /** Committed non-secret variables; the public toggle is the only one. */
  readonly vars?: Record<string, string>;
  readonly r2_buckets: ReadonlyArray<{
    readonly binding: string;
    readonly bucket_name: string;
    readonly preview_bucket_name?: string;
    readonly public_bucket?: boolean;
    readonly preview_bucket?: boolean;
  }>;
}

/** Reads the deployment base config exactly as committed. */
function readConfig(): WranglerConfig {
  return JSON.parse(wranglerSource) as WranglerConfig;
}

describe('deployment base config', () => {
  it('cannot expose the worker on a public workers.dev hostname', () => {
    const config = readConfig();
    expect(config.workers_dev).toBe(false);
    expect(config.preview_urls).toBe(false);
  });

  it('declares no route in the base config until the operator supplies a domain', () => {
    expect(readConfig().routes).toBeUndefined();
  });

  it('binds one R2 bucket that is neither public nor previewable', () => {
    const [bucket, ...rest] = readConfig().r2_buckets;
    expect(rest).toEqual([]);
    expect(bucket?.binding).toBe('BUCKET');
    expect(bucket?.public_bucket).toBeUndefined();
    expect(bucket?.preview_bucket).toBeUndefined();
    expect(bucket?.preview_bucket_name).toBeUndefined();
  });

  it('binds the known production bucket without a placeholder', () => {
    const [bucket] = readConfig().r2_buckets;
    expect(bucket?.bucket_name).toBe('apt');
  });

  it('pins the known production account', () => {
    expect(readConfig().account_id).toBe('12412d5b061683494ea3e5c217d6a961');
  });

  it('binds exactly the non-secret public AUTH_REQUIRED text toggle', () => {
    const config = readConfig();
    expect(config.vars).toEqual({ AUTH_REQUIRED: 'false' });
    expect(typeof config.vars?.AUTH_REQUIRED).toBe('string');
    for (const forbidden of ['api_token', 'API_TOKEN', 'CREDENTIALS', 'secret']) {
      expect(wranglerSource, forbidden).not.toContain(forbidden);
    }
  });

  it('carries no endpoint in the base config', () => {
    for (const forbidden of ['r2.dev', 'http://', 'https://']) {
      expect(wranglerSource, forbidden).not.toContain(forbidden);
    }
  });

  it('preserves dashboard plaintext vars across deployments', () => {
    expect(readConfig().keep_vars).toBe(true);
  });

  it('names the worker entry point that the gateway exports', () => {
    expect(readConfig().main).toBe('src/index.ts');
  });

  it('is not dated in the future', () => {
    const declared = readConfig().compatibility_date;
    expect(declared).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(Date.parse(`${declared}T00:00:00Z`)).toBeLessThanOrEqual(Date.now());
  });
});
