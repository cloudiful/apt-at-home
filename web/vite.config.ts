import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

/**
 * Builds the browser assets of the public landing page.
 *
 * The build writes flat files into the Worker's asset directory, so every
 * public URL is `/assets/<filename>` and the Worker can serve exactly one
 * flat name. The manifest records the hashed names of the entry script and
 * its stylesheet, and the Worker imports that manifest to link them.
 */
export default defineConfig({
  root: '.',
  publicDir: 'web/public',
  resolve: {
    alias: {
      // The alias points at the shared source tree, next to this config.
      '@': new URL('./src', import.meta.url).pathname,
    },
  },
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'worker/dist/assets',
    emptyOutDir: true,
    manifest: true,
    sourcemap: false,
    // Assets sit directly in the output directory, so their public URLs stay
    // flat and the Worker can validate a single file name.
    assetsDir: '.',
    rollupOptions: {
      input: 'web/src/main.tsx',
      output: {
        // The names carry the content hash, so a changed build never keeps the
        // name of the old file and a cached document always links the bytes it
        // was rendered with.
        entryFileNames: '[name]-[hash].js',
        chunkFileNames: '[name]-[hash].js',
        assetFileNames: '[name]-[hash][extname]',
      },
    },
  },
})
