// The web app is an adapter-static SPA. Disable SvelteKit SSR so browser-only
// Markdown sanitization (DOMPurify) never runs during server rendering/build.
export const ssr = false;
