import { defineConfig } from 'vite';

// Relative base so the built site works from any sub-path —
// GitHub Pages project sites (user.github.io/<repo>/), custom domains, or local preview.
export default defineConfig({
  base: './',
});
