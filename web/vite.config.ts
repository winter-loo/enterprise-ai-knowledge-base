import { defineConfig } from 'vitest/config';
import tailwindcss from '@tailwindcss/vite';
import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			adapter: adapter({
				fallback: 'index.html'
			})
		})
	],
	server: {
		proxy: {
			'/api/v1/authz': {
				target: 'http://127.0.0.1:8012',
				configure(proxy) {
					proxy.on('proxyReq', (request) => {
						request.setHeader('x-principal', process.env.AUTHZ_DEV_PRINCIPAL?.trim() || 'admin');
					});
				}
			},
			'/api/v1/chat': 'http://127.0.0.1:8011',
			'/api': 'http://127.0.0.1:8010'
		}
	},
	test: {
		expect: { requireAssertions: true },
		environment: 'node',
		include: ['src/**/*.{test,spec}.{js,ts}'],
		exclude: ['src/**/*.svelte.{test,spec}.{js,ts}']
	}
});
