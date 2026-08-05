import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  // Vite's dependency scanner globs for every *.html by default -- without
  // this it also crawls prototypes/dot_sphere_prototype.html (a standalone
  // three.js reference file loaded via a CDN importmap, not part of this
  // app) and tries to resolve its d3-geo-voronoi CDN import as an npm
  // package, breaking the real app's dependency pre-bundling.
  optimizeDeps: {
    entries: ['index.html'],
  },
})
