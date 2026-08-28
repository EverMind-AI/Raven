import { Badge } from '@/components/ui/badge';
import { Spinner } from '@/components/ui/spinner';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import CheckCircle from '~icons/solar/check-circle-bold-duotone';
import XCircle from '~icons/solar/close-circle-bold-duotone';

/**
 * Displays a status badge with icon and label based on execution status.
 * @param root0 - Component props.
 * @param root0.className - Optional CSS class.
 * @param root0.status - The execution status to display.
 * @returns A styled Badge component.
 */
export function StatusBadge({
	className,
	status,
}: {
	className?: string;
	status: 'running' | 'completed' | 'failed';
}) {
	const { t } = useTranslation();
	switch (status) {
		case 'running':
			return (
				<Badge variant="secondary" className={className}>
					<Spinner data-icon="inline-start" />
					{t('common.running')}
				</Badge>
			);
		case 'completed':
			return (
				<Badge className={cn('icon-mono bg-gold-olive/10 text-gold-olive', className)}>
					<CheckCircle className="size-5" />
					{t('common.completed')}
				</Badge>
			);
		case 'failed':
			return (
				<Badge className={cn('icon-mono bg-destructive/10 text-destructive border-none')}>
					<XCircle className="size-5" />
					{t('common.failed')}
				</Badge>
			);
	}
}
