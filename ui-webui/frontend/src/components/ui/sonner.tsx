import { useTheme } from 'next-themes';
import { Toaster as Sonner, type ToasterProps } from 'sonner';
import CircleCheckIcon from '~icons/solar/check-circle-linear';
import InfoIcon from '~icons/solar/info-circle-bold-duotone';
import Loader2Icon from '~icons/solar/refresh-linear';
import OctagonXIcon from '~icons/solar/close-circle-bold-duotone';
import TriangleAlertIcon from '~icons/solar/danger-triangle-bold-duotone';

const Toaster = ({ ...props }: ToasterProps) => {
	const { resolvedTheme } = useTheme();

	return (
		<Sonner
			theme={resolvedTheme as ToasterProps['theme']}
			className="toaster group"
			icons={{
				success: <CircleCheckIcon className="size-4" />,
				info: <InfoIcon className="size-4" />,
				warning: <TriangleAlertIcon className="size-4" />,
				error: <OctagonXIcon className="size-4" />,
				loading: <Loader2Icon className="size-4 animate-spin" />,
			}}
			style={
				{
					'--normal-bg': 'var(--popover)',
					'--normal-text': 'var(--popover-foreground)',
					'--normal-border': 'var(--border)',
					'--border-radius': '12px',
					'--success-bg': 'var(--popover)',
					'--success-text': 'var(--gold-olive)',
					'--error-bg': 'var(--popover)',
					'--error-text': 'var(--destructive)',
				} as React.CSSProperties
			}
			{...props}
		/>
	);
};

export { Toaster };
