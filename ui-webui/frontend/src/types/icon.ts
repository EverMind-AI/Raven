import type { FC, SVGProps } from 'react';

// Icon components (unplugin-icons / Solar) accept standard SVG props. Used where
// a component takes an icon as a prop.
export type IconComponent = FC<SVGProps<SVGSVGElement>>;
