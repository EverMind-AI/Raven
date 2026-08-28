import * as React from 'react';
import { Switch as SwitchPrimitive } from 'radix-ui';

import { cn } from '@/lib/utils';

/** Colour scheme, both themes:
 *
 *  | state | track | thumb | thumb vs track |
 *  |---|---|---|---|
 *  | on, light | `primary` #1c1b1b | `background` #fcfcf5 | 16.9:1 |
 *  | on, dark | `primary` #f2f1e9 | `primary-foreground` #1c1b1b | 16.9:1 |
 *  | off, light | `muted-foreground/40` ~#b8b4a9 | `background` + ring | 2.0:1, ring edge 3.2:1 |
 *  | off, dark | `muted-foreground/35` ~#4e4d4a | `foreground` #f2f1e9 | 7.5:1 |
 *
 *  Two things this replaces, both of which made the off state hard to see. The
 *  track was `input` -- a 12% black that composites to roughly #dbdad2 on a light
 *  surface, only 1.34:1 against the near-white thumb, so the control read as an
 *  empty rounded rectangle. And the thumb had no outline, so a near-white circle
 *  on a light track had no edge at all; the `ring` is what carries the off state's
 *  contrast now, since a track dark enough to reach 3:1 on its own would read as
 *  "on" in a design where dark means on.
 *
 *  The track colours are `!important` so an ambient wrapper rule cannot repaint
 *  them. This is not hypothetical: hosting this switch in `SidebarMenuAction`
 *  brings `hover:bg-sidebar-accent` along, which has the same specificity as
 *  `data-checked:bg-primary` and wins on source order -- hovering an enabled
 *  switch blanked its track to the page colour. Same reasoning as
 *  `[&_svg]:!size-full` in `SubagentIcon`: a component's own state must outrank
 *  the styling of wherever it happens to be mounted.
 */
function Switch({
	className,
	size = 'default',
	...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & {
	size?: 'sm' | 'default';
}) {
	return (
		<SwitchPrimitive.Root
			data-slot="switch"
			data-size={size}
			className={cn(
				'peer group/switch relative inline-flex shrink-0 items-center rounded-full border border-transparent transition-all outline-none after:absolute after:-inset-x-3 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 data-[size=default]:h-[18.4px] data-[size=default]:w-[32px] data-[size=sm]:h-[14px] data-[size=sm]:w-[24px] dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40 data-checked:bg-primary! data-unchecked:border-muted-foreground/45 data-unchecked:bg-muted-foreground/40! dark:data-unchecked:border-muted-foreground/40 dark:data-unchecked:bg-muted-foreground/35!',
				'data-disabled:cursor-not-allowed data-disabled:opacity-50',
				className,
			)}
			{...props}
		>
			{/* The thumb is 2px smaller than the track's content box and offset by
			    1px at both ends, so it reads as a circle inside the track. At the
			    previous sizes it was exactly as tall as the content box and sat at
			    translate-x-0, flush against the track's own rounded edge, which
			    made it look like a clipped rectangle. Offsets are explicit pixels
			    rather than `calc(100% +/- n)`, which is easy to get subtly wrong.
			    They are measured from the content box's leading edge, so a wrapper
			    contributing `justify-center` shifts both states rightward -- pass
			    `justify-start` at such a call site. */}
			<SwitchPrimitive.Thumb
				data-slot="switch-thumb"
				className="pointer-events-none block rounded-full bg-background ring-1 ring-foreground/25 transition-transform group-data-[size=default]/switch:size-3.5 group-data-[size=sm]/switch:size-2.5 group-data-[size=default]/switch:data-checked:translate-x-[15px] group-data-[size=sm]/switch:data-checked:translate-x-[11px] dark:data-checked:bg-primary-foreground group-data-[size=default]/switch:data-unchecked:translate-x-px group-data-[size=sm]/switch:data-unchecked:translate-x-px dark:data-unchecked:bg-foreground"
			/>
		</SwitchPrimitive.Root>
	);
}

export { Switch };
