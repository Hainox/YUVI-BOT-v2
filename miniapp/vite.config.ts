import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// Mini App SPA: adapter-static with a fallback page since every route is
			// client-rendered (ssr=false in +layout.ts) and Telegram deep-links can
			// land on any nested route (/games/coinflip, etc.) directly.
			adapter: adapter({ pages: 'build', assets: 'build', fallback: '200.html', strict: true })
		})
	]
});
