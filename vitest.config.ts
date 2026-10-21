import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    // The same alias the web build and the Worker bundle use, so the tests
    // load the very modules the page is built from.
    alias: {
      '@': new URL('./web/src', import.meta.url).pathname,
    },
  },
  test: {
    include: ['worker/test/**/*.test.ts'],
    environment: 'node',
    globals: false,
  },
});
