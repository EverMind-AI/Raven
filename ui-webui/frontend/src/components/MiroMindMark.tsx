import type { SVGProps } from 'react';

/** MiroMind's brand mark, inlined from https://dr.miromind.ai/favicon.svg.
 *
 *  Inlined as a component rather than added as an `.svg` file because AGENTS.md
 *  section 7 keeps image assets out of the repo. The source's white card is kept,
 *  so the mark carries its own backing in both themes: the wordmark's dark stroke
 *  is only legible on white, which is why it has no `dark:` variant (the
 *  original's `.dark-letter` / `prefers-color-scheme` rule is dropped for the
 *  same reason, along with its generic class names - `bg`, `border`,
 *  `dark-letter` - that would otherwise leak into the page's global scope). The
 *  card is drawn square and left for `SubagentIcon`'s tile to clip, so every
 *  brand mark in the list shares one silhouette instead of each bringing its
 *  own corner radius. */
export function MiroMindMark(props: SVGProps<SVGSVGElement>) {
	return (
		<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" {...props}>
			<rect width="64" height="64" fill="#FFFFFF" />
			<g transform="translate(32, 32) scale(0.2) translate(-131.5, -100)">
				<path
					fill="#00CCB5"
					d="M35.0571 180.475C33.7196 184.05 30.4696 186.675 26.5446 187.337C22.6196 188 18.6196 186.6 16.0821 183.675C13.5446 180.75 12.8446 176.75 14.2571 173.2L73.2196 22.9247C75.682 16.6622 81.9696 12.5122 88.9946 12.5122C96.0195 12.5122 102.307 16.6622 104.757 22.9247L163.72 173.2C166.42 180.1 161.07 187.437 153.345 187.437C148.682 187.437 144.52 184.65 142.932 180.475L89.9696 40.8247C89.8195 40.4372 89.4321 40.1747 88.9946 40.1747C88.5571 40.1747 88.1695 40.4372 88.0195 40.8247L35.0571 180.475Z"
				/>
				<path
					fill="#002B47"
					d="M121.55 180.525C119.937 184.7 115.762 187.462 111.1 187.438C103.375 187.438 98.0247 180.25 100.675 173.475L159.8 22.7625C162.225 16.6 168.5 12.5 175.525 12.5C182.537 12.5 188.812 16.6 191.225 22.7625L250.35 173.475C253.012 180.25 247.65 187.438 239.937 187.438C235.275 187.462 231.087 184.7 229.487 180.525L176.487 40.825C176.337 40.4375 175.95 40.175 175.512 40.175C175.075 40.175 174.687 40.4375 174.537 40.825L121.562 180.525H121.55Z"
				/>
			</g>
		</svg>
	);
}
