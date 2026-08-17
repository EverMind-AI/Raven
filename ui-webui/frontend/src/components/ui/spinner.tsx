import { cn } from '@/lib/utils';
import Loader2Icon from '~icons/solar/refresh-linear';

function Spinner({ className, ...props }: React.ComponentProps<'svg'>) {
	return (
		<Loader2Icon
			role="status"
			aria-label="Loading"
			className={cn('size-4 animate-spin', className)}
			{...props}
		/>
	);
}

export { Spinner };
