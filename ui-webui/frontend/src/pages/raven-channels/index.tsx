import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenChannel } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import IconExternal from '~icons/solar/arrow-right-up-linear';
import IconBuildings from '~icons/solar/buildings-bold-duotone';
import IconWeixin from '~icons/solar/chat-round-bold-duotone';
import IconFeishu from '~icons/solar/chat-round-dots-bold-duotone';
import IconQQ from '~icons/solar/chat-square-2-bold-duotone';
import IconChannels from '~icons/solar/chat-square-bold-duotone';
import IconCheck from '~icons/solar/check-circle-bold';
import IconClose from '~icons/solar/close-circle-bold';
import IconMochat from '~icons/solar/dialog-2-bold-duotone';
import IconDingtalk from '~icons/solar/dialog-bold-duotone';
import IconDiscord from '~icons/solar/ghost-smile-bold-duotone';
import IconSlack from '~icons/solar/hashtag-bold-duotone';
import IconMatrix from '~icons/solar/hashtag-square-bold-duotone';
import IconEmail from '~icons/solar/letter-bold-duotone';
import IconTelegram from '~icons/solar/plain-2-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import IconSmartphone from '~icons/solar/smartphone-bold-duotone';

/**
 * Presentation for each channel: friendly label, gradient tile, glyph. The
 * backend returns only the raw registry name (`telegram`), so the display name /
 * icon / brand color are mapped here. One-line blurbs are i18n keys
 * (`ravenChannels.blurbs.<name>`).
 */
type ChannelMeta = { label: string; Icon: typeof IconChannels; tile: string };

const CHANNEL_META: Record<string, ChannelMeta> = {
	telegram: { label: 'Telegram', Icon: IconTelegram, tile: 'from-sky-400 to-blue-600' },
	discord: { label: 'Discord', Icon: IconDiscord, tile: 'from-indigo-400 to-violet-600' },
	feishu: { label: 'Feishu / Lark', Icon: IconFeishu, tile: 'from-cyan-400 to-teal-600' },
	dingtalk: { label: 'DingTalk', Icon: IconDingtalk, tile: 'from-blue-400 to-sky-600' },
	slack: { label: 'Slack', Icon: IconSlack, tile: 'from-fuchsia-400 to-purple-600' },
	wecom: { label: 'WeCom', Icon: IconBuildings, tile: 'from-emerald-400 to-green-600' },
	weixin: { label: 'WeChat', Icon: IconWeixin, tile: 'from-green-400 to-emerald-600' },
	qq: { label: 'QQ', Icon: IconQQ, tile: 'from-sky-400 to-indigo-600' },
	whatsapp: { label: 'WhatsApp', Icon: IconSmartphone, tile: 'from-green-400 to-teal-600' },
	matrix: { label: 'Matrix', Icon: IconMatrix, tile: 'from-slate-400 to-gray-600' },
	mochat: { label: 'MoChat', Icon: IconMochat, tile: 'from-amber-400 to-orange-600' },
	email: { label: 'Email', Icon: IconEmail, tile: 'from-rose-400 to-red-600' },
};

function metaFor(name: string): ChannelMeta {
	return (
		CHANNEL_META[name] ?? {
			label: name,
			Icon: IconChannels,
			tile: 'from-slate-400 to-gray-600',
		}
	);
}

/** Brand logos (CC0 simple-icons path data) inlined as raw SVG bodies, keyed
 *  by channel name. Channels without a brand entry fall back to a solar glyph.
 *  The solar-only icon setup has no brand collection, so these are embedded
 *  rather than pulled from an icon dependency. */
const BRAND_ICONS: Record<string, string> = {
	dingtalk: '<path fill="currentColor" fill-rule="evenodd" d="M6.802 2.02a1 1 0 0 1 .849.22l9.751 8.359a2 2 0 0 1 .235 2.799l-1.06 1.272l.87.436a1 1 0 0 1 .134 1.708l-7 5a1 1 0 0 1-1.539-1.101l1.21-4.034c-2.363-.9-3.747-3.055-4.233-5.483A1 1 0 0 1 7.01 10c-.474-.703-.86-1.42-1.134-2.149c-.649-1.73-.658-3.523.23-5.298a1 1 0 0 1 .696-.533" clip-rule="evenodd"/>',
	wecom: '<path fill="currentColor" d="M12 1c6.075 0 11 4.925 11 11s-4.925 11-11 11S1 18.075 1 12S5.925 1 12 1m3.52 15.49a.35.35 0 0 0-.24.1c-.14.13-.16.34.02.53l.07.07c.44.44.74.99.85 1.57c0 .02.04.23.04.23c.05.19.15.37.29.5c.21.21.51.34.82.34c.3 0 .59-.12.8-.33c.44-.44.44-1.16 0-1.61c-.15-.15-.34-.26-.53-.3l-.15-.03c-.61-.11-1.17-.41-1.62-.86c-.03-.03-.07-.07-.1-.11c-.06-.074-.16-.1-.25-.1M11 4.75c-2.117 0-4.264.77-5.75 2.31C4.111 8.246 3.5 9.72 3.5 11.24c0 1.06.3 2.12.88 3.06c.47.695.993 1.371 1.66 1.89l-.384 1.624a.6.6 0 0 0 .856.673L8.64 17.41c.53.166 1.08.234 1.63.3a8.3 8.3 0 0 0 1.7-.03l.38-.05q.283-.046.564-.112a2.33 2.33 0 0 1-.92-1.605l-.254.037c-.62.067-1.232.03-1.85-.04c-.43-.057-.838-.185-1.25-.31l-1.02.5l.23-.67l-.74-.6c-.513-.401-.917-.934-1.28-1.47c-.4-.65-.61-1.38-.61-2.11c0-1.08.456-2.119 1.26-2.97c1.158-1.198 2.854-1.78 4.5-1.78c1.54 0 3.108.513 4.24 1.58c.365.365.707.75.95 1.21c.177.354.338.722.424 1.107a2.34 2.34 0 0 1 1.811.123c-.075-.716-.33-1.4-.665-2.04c-.329-.62-.776-1.155-1.27-1.65c-1.468-1.38-3.471-2.08-5.47-2.08m9.37 9.77a1.136 1.136 0 0 0-1.1.86l-.03.15a3.1 3.1 0 0 1-.86 1.63c-.04.03-.07.07-.11.1c-.14.13-.14.35 0 .49c.07.06.17.1.26.1h.01c.07 0 .15-.02.26-.13l.07-.07c.44-.44.99-.74 1.57-.85c.023 0 .227-.04.23-.04c.2-.06.37-.16.5-.3c.44-.44.44-1.17 0-1.61c-.21-.21-.5-.33-.8-.33m-4.21-1.07c-.08 0-.16.03-.27.14l-.07.07c-.44.44-.99.74-1.57.85c-.02 0-.23.04-.23.04c-.2.06-.37.16-.5.3c-.44.44-.44 1.17 0 1.61c.21.21.51.34.82.34c.3 0 .59-.12.8-.33c.15-.16.25-.34.29-.53a.4.4 0 0 0 .03-.16c.11-.61.41-1.18.86-1.63c.03-.03.06-.06.1-.09c.146-.115.13-.36 0-.49a.34.34 0 0 0-.26-.12m1.18-1.97c-.3 0-.59.12-.8.33c-.44.44-.44 1.16 0 1.61c.15.15.34.26.53.3c.054.006.144.029.15.03c.61.12 1.17.41 1.62.86c.03.03.07.07.1.11c.08.08.16.1.25.1c.1 0 .16-.04.23-.11c.12-.13.14-.32-.02-.52l-.08-.08c-.44-.44-.74-.99-.85-1.57c0-.02-.04-.23-.04-.23c-.05-.19-.15-.37-.29-.5c-.21-.21-.5-.33-.8-.33"/>',
	telegram: '<path fill="currentColor" d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12a12 12 0 0 0 12-12A12 12 0 0 0 12 0zm4.962 7.224c.1-.002.321.023.465.14a.5.5 0 0 1 .171.325c.016.093.036.306.02.472c-.18 1.898-.962 6.502-1.36 8.627c-.168.9-.499 1.201-.82 1.23c-.696.065-1.225-.46-1.9-.902c-1.056-.693-1.653-1.124-2.678-1.8c-1.185-.78-.417-1.21.258-1.91c.177-.184 3.247-2.977 3.307-3.23c.007-.032.014-.15-.056-.212s-.174-.041-.249-.024q-.159.037-5.061 3.345q-.72.495-1.302.48c-.428-.008-1.252-.241-1.865-.44c-.752-.245-1.349-.374-1.297-.789q.04-.324.893-.663q5.247-2.286 6.998-3.014c3.332-1.386 4.025-1.627 4.476-1.635"/>',
	discord: '<path fill="currentColor" d="M20.317 4.37a19.8 19.8 0 0 0-4.885-1.515a.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.3 18.3 0 0 0-5.487 0a13 13 0 0 0-.617-1.25a.08.08 0 0 0-.079-.037A19.7 19.7 0 0 0 3.677 4.37a.1.1 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.08.08 0 0 0 .031.057a19.9 19.9 0 0 0 5.993 3.03a.08.08 0 0 0 .084-.028a14 14 0 0 0 1.226-1.994a.076.076 0 0 0-.041-.106a13 13 0 0 1-1.872-.892a.077.077 0 0 1-.008-.128a10 10 0 0 0 .372-.292a.07.07 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.07.07 0 0 1 .078.01q.181.149.373.292a.077.077 0 0 1-.006.127a12.3 12.3 0 0 1-1.873.892a.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.08.08 0 0 0 .084.028a19.8 19.8 0 0 0 6.002-3.03a.08.08 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.06.06 0 0 0-.031-.03M8.02 15.33c-1.182 0-2.157-1.085-2.157-2.419c0-1.333.956-2.419 2.157-2.419c1.21 0 2.176 1.096 2.157 2.42c0 1.333-.956 2.418-2.157 2.418m7.975 0c-1.183 0-2.157-1.085-2.157-2.419c0-1.333.955-2.419 2.157-2.419c1.21 0 2.176 1.096 2.157 2.42c0 1.333-.946 2.418-2.157 2.418"/>',
	slack: '<path fill="currentColor" d="M5.042 15.165a2.53 2.53 0 0 1-2.52 2.523A2.53 2.53 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52a2.527 2.527 0 0 1 2.521 2.52v6.313A2.53 2.53 0 0 1 8.834 24a2.53 2.53 0 0 1-2.521-2.522zM8.834 5.042a2.53 2.53 0 0 1-2.521-2.52A2.53 2.53 0 0 1 8.834 0a2.53 2.53 0 0 1 2.521 2.522v2.52zm0 1.271a2.53 2.53 0 0 1 2.521 2.521a2.53 2.53 0 0 1-2.521 2.521H2.522A2.53 2.53 0 0 1 0 8.834a2.53 2.53 0 0 1 2.522-2.521zm10.122 2.521a2.53 2.53 0 0 1 2.522-2.521A2.53 2.53 0 0 1 24 8.834a2.53 2.53 0 0 1-2.522 2.521h-2.522zm-1.268 0a2.53 2.53 0 0 1-2.523 2.521a2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.53 2.53 0 0 1 2.523 2.522zm-2.523 10.122a2.53 2.53 0 0 1 2.523 2.522A2.53 2.53 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522zm0-1.268a2.527 2.527 0 0 1-2.52-2.523a2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.53 2.53 0 0 1-2.522 2.523z"/>',
	whatsapp: '<path fill="currentColor" d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967c-.273-.099-.471-.148-.67.15c-.197.297-.767.966-.94 1.164c-.173.199-.347.223-.644.075c-.297-.15-1.255-.463-2.39-1.475c-.883-.788-1.48-1.761-1.653-2.059c-.173-.297-.018-.458.13-.606c.134-.133.298-.347.446-.52s.198-.298.298-.497c.099-.198.05-.371-.025-.52s-.669-1.612-.916-2.207c-.242-.579-.487-.5-.669-.51a13 13 0 0 0-.57-.01c-.198 0-.52.074-.792.372c-.272.297-1.04 1.016-1.04 2.479c0 1.462 1.065 2.875 1.213 3.074s2.096 3.2 5.077 4.487c.709.306 1.262.489 1.694.625c.712.227 1.36.195 1.871.118c.571-.085 1.758-.719 2.006-1.413s.248-1.289.173-1.413c-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214l-3.741.982l.998-3.648l-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884c2.64 0 5.122 1.03 6.988 2.898a9.82 9.82 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.82 11.82 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.9 11.9 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.82 11.82 0 0 0-3.48-8.413"/>',
	matrix: '<path fill="currentColor" d="M.632.55v22.9H2.28V24H0V0h2.28v.55zm7.043 7.26v1.157h.033a3.3 3.3 0 0 1 1.117-1.024c.433-.245.936-.365 1.5-.365q.81.002 1.481.314c.448.208.785.582 1.02 1.108q.382-.562 1.034-.992q.651-.43 1.546-.43q.679 0 1.26.167c.388.11.716.286.993.53c.276.245.489.559.646.951q.229.587.23 1.417v5.728h-2.349V11.52q0-.43-.032-.812a1.8 1.8 0 0 0-.18-.66a1.1 1.1 0 0 0-.438-.448q-.292-.165-.785-.166q-.498 0-.803.189a1.4 1.4 0 0 0-.48.499a2 2 0 0 0-.231.696a6 6 0 0 0-.06.785v4.768h-2.35v-4.8q.002-.38-.018-.752a2.1 2.1 0 0 0-.143-.688a1.05 1.05 0 0 0-.415-.503c-.194-.125-.476-.19-.854-.19q-.168 0-.439.074c-.18.051-.36.143-.53.282a1.64 1.64 0 0 0-.439.595q-.18.39-.18 1.02v4.966H5.46V7.81zm15.693 15.64V.55H21.72V0H24v24h-2.28v-.55z"/>',
	weixin: '<path fill="currentColor" d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 0 1 .213.665l-.39 1.48c-.019.07-.048.141-.048.213c0 .163.13.295.29.295a.33.33 0 0 0 .167-.054l1.903-1.114a.86.86 0 0 1 .717-.098a10.2 10.2 0 0 0 2.837.403c.276 0 .543-.027.811-.05c-.857-2.578.157-4.972 1.932-6.446c1.703-1.415 3.882-1.98 5.853-1.838c-.576-3.583-4.196-6.348-8.596-6.348M5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178A1.17 1.17 0 0 1 4.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 0 1-1.162 1.178a1.17 1.17 0 0 1-1.162-1.178c0-.651.52-1.18 1.162-1.18m5.34 2.867c-1.797-.052-3.746.512-5.28 1.786c-1.72 1.428-2.687 3.72-1.78 6.22c.942 2.453 3.666 4.229 6.884 4.229c.826 0 1.622-.12 2.361-.336a.72.72 0 0 1 .598.082l1.584.926a.3.3 0 0 0 .14.047c.134 0 .24-.111.24-.247c0-.06-.023-.12-.038-.177l-.327-1.233a.6.6 0 0 1-.023-.156a.49.49 0 0 1 .201-.398C23.024 18.48 24 16.82 24 14.98c0-3.21-2.931-5.837-6.656-6.088V8.89c-.135-.01-.27-.027-.407-.03zm-2.53 3.274c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983a.976.976 0 0 1-.969-.983c0-.542.434-.982.97-.982zm4.844 0c.535 0 .969.44.969.982a.976.976 0 0 1-.969.983a.976.976 0 0 1-.969-.983c0-.542.434-.982.969-.982"/>',
	qq: '<path fill="currentColor" d="M21.395 15.035a40 40 0 0 0-.803-2.264l-1.079-2.695c.001-.032.014-.562.014-.836C19.526 4.632 17.351 0 12 0S4.474 4.632 4.474 9.241c0 .274.013.804.014.836l-1.08 2.695a39 39 0 0 0-.802 2.264c-1.021 3.283-.69 4.643-.438 4.673c.54.065 2.103-2.472 2.103-2.472c0 1.469.756 3.387 2.394 4.771c-.612.188-1.363.479-1.845.835c-.434.32-.379.646-.301.778c.343.578 5.883.369 7.482.189c1.6.18 7.14.389 7.483-.189c.078-.132.132-.458-.301-.778c-.483-.356-1.233-.646-1.846-.836c1.637-1.384 2.393-3.302 2.393-4.771c0 0 1.563 2.537 2.103 2.472c.251-.03.581-1.39-.438-4.673"/>',
	email: '<path fill="currentColor" d="M24 5.457v13.909c0 .904-.732 1.636-1.636 1.636h-3.819V11.73L12 16.64l-6.545-4.91v9.273H1.636A1.636 1.636 0 0 1 0 19.366V5.457c0-2.023 2.309-3.178 3.927-1.964L5.455 4.64L12 9.548l6.545-4.91l1.528-1.145C21.69 2.28 24 3.434 24 5.457"/>',
};

/** Render a brand logo from its raw SVG body. The body is static CC0 icon
 *  data (never user input), so dangerouslySetInnerHTML is safe here. */
function BrandGlyph({ body, className }: { body: string; className?: string }) {
	return (
		<svg
			viewBox="0 0 24 24"
			className={className}
			dangerouslySetInnerHTML={{ __html: body }}
		/>
	);
}

/** Colored raster brand logos (data URI) for channels with no monochrome glyph
 *  in the open icon sets (e.g. Feishu). Rendered on a white tile since they
 *  carry their own color. */
const BRAND_IMAGES: Record<string, string> = {
	mochat: 'data:image/png;base64,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',
	feishu: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAAM6UlEQVR42u2be7CcZX3HP7/ned/dPWfPNSdAkLbBsSUYLkESygjEw5mKCZE6Q509zvQfx0JB0IANjjpqu1mdgi0DVKRasNpSHdSzrUorodoyJ0douEYLLTHQWoSAXHJyOde9vO/z/PrHu+cSQ+Bcdk+Sdr8zO7Ozt+d5v+/3d3l+v99CE0000UQTTTTRRBNNNNFEE038/4LU7ZdUDWw3bF+inV8MFPcq/f2ueRsXg3zeHN8K1LxBCp6f/WgDXW1nMzwaERDgG7hrY5R0KqJSfoK3b9xBPm8oFPzRIDBY/E+cIYCg/jcIwj/n5OUwWYIgANUG797Cz354B2/fsJnBwYC+vvj49IFTCnjqvnW0ZL5BGJ7O+EQZkQABtK7etqZ8QASWdwe8uu8TnHXpzUeDxDoGkQGL9Dse/W4P3R1305Z9LwdGHahBRBqye1XFGkcYGiqlC1m96REGBuxSBpb6OWDpd+iA5fzf28dp776M8fFb6GyzGKP4BtmyiBB7g4gB+7e8sKOlRqwcfwROkZjPG1QNp13ycUZHP0o6ZQitoNoYJ2/EUCrFdHesYmTkTxL1Fc3xZ8KH+ieBQYv0xTy17XJaM/egmqFS9RhjGrCeYo0nMI44WsOqDc8Agog/vhQ4c1sU6Yt54s6Qszd9j8nyRoyM0JIxqLoGrCc4B5lMish/ARGlWJTjV4GHqGMwSJR43zqyLfeDLKdccYjYBqzmSacMsTufVe9+bCkCSuN9hfTF6GDA2e99gtLkJagOk0lbvG+AeaknFUIUf/r49oGvh6kc7cn7z6U19QBIV+ITpd43UQmsoxyfxdkbd6NqGukLl+4c2VdT4ppLf8J4+TKMTBIG9Y/Oqo621oBArgBg+3bzf0OBh/nEbZvItvwTUaw4V89k25MKDZXKHkbGVnFBfwlFEPTQaz7keKQNU2AupxbqmJjOROdtlCt/SLbFYsTV1aoqVU82++t0da6HvOH2bSl68wHkTY0sBdGZ53lDbz6Y9ZlGKFCltmh98MSdIeuujtj1z5+nq/OzHBiJEQnqZMYxHe2W0bGvcMbGj/zq2yt7/yYzyUTQSjZ+fuhD5de91t6tlqGt7s2u+U0JPPc6vdyNMPjk3XKQvBoKdXPIwuCgpa8v5ukf/j1d7e/n4EiMmIWTqIBXCMXT3WIYKe9e9uE976fK+Yieh7JK8CeDtCsaCBKDjinyMmJ2gz5uNNwx/NAHn62bAs+9XoeNZZeO8zs77yImj9SNxOTMKjz5oxYy8hjp9GomJhd2WvG14lyLwbwWk3pkgvQj4xo8X0GCrICAOlCHokmpTQRBQGzyQFFXjlB5HOEeF+sPDj68Zw8cudZo5sDwi2Er68nyD2uvIqAgnrzWJ7JNnRjO2TBBHOeI3SSplKLzKD5ojbw2g5SV1nv20/WZl2j7+jDhsxUBEY0mY40mYo1LTn3k8bFHY8XHXn3kNS45jSZijSYcqqEYucCme+6w1hdgq5IbsAtPY4RUNIm3KX6XLPeu/KBmKIhPgksd0N/vGBwMOHPTLibL19KasQhuzuSZhLzUQ+N0feYlWr9zABl3aIdFMzJ1Mg+Sh9jaN0xSTKw9F7HT76tzYHDlvQ/jzWbYKhRzC1cgiorBuDLVIM2lJ/Swbe1V2lksiqsbidM54qa7OTj2DTo7AvybnJk9ECSWmb1rLx23vordF6OdFqyA06nkROblCExKVN2wl+oH9u24cqymIq1HIh1EJWIT0iftPLDmaj2lWBTXm9f6RE4udqgaUv6jjI0/T0vaHDHJ9kBakJKn8wuv0HrfCNpm0PAQ4hZUoBUTGPXR1Qce/MieJK15417L/HIeIYjLxMayNszy4Jpr9ZyhgsR1IVFEoSictmmUqrsSYyR57QjkjTk6b3qZ8KkSvjtIXtdFBTQnYav1cen7+//tw9+lNx8wVHjT9oCZ/3USxBWcGN4atjC07np931BB4lxO7aIrwVLzh2df+q+MT95FZ7s9xJQ9kBHMqKPzplcI/ruCtluIF52eKmJEXbVqRD4JKlw8t76iWZhYsK6KR+mQkHvXfkw/VSyKQ0QX7RcvrplyXP4kYxO/JJMWwONq5B10dNz4CsH/VNA2k5js4ulzErQaNPr28ENXPUuuOOc26YLTETEYdaiP8UGGm9Zt0e+cdY12L9ovTpnyOy4/SBTdQCZliFVpEcyBGnm/mCKvTtUbEaOuHOGDG0GF1U/P+a4sLp8TBDBRidik6M+08vDa6/SioYLEqMqC88WpBtVZm77N8OgQKzqs2R+7jhtfJnihruTNVt+39u248pn5qG9OBM4lpokQxCVihFUSsn3tH+lnEaAgPlHjQnxjLklg13RdY16cGOu46TUJXqiqZutI3oz6qoL5U2Be6oO6TCbMkOgivAgmyPD5dVt4T6y6eaggT05VdYrFeVRdLt5uGOqPT1z/5UqcTk+YCW2rM3mJ+sLWQKvj3xrecfWz5AYshfm1AOpabBRJfi8qEZuA9YHwyHlb9I9/c7OmpxPvNzVrlSSF6Iu7L/rKhU4yO0zFrNDQK76u9csZ9RmZt+9rZEVaplId9WRMis91pXh03Q16abEojoL43MARUp7cgAVRhgrxsgvv/AMj6QdEOEmperTOe1VNfJ+P7plv5F2Skr4IFtCoRCzCGjFsW7dFi+uu0zOL/UnK05vXICEyb8gNWIr9rueCv27vWf+1u0zY+jXUpdVFHhrQNxFj1JWqYnXB6ptrOes/bcgZPsIjCyNcFS+AzWB8RBnhqw5u+ekt8vzsz3Vf9NWNIvY2Y9OnazTukvUaMFejxBJmA3z5r4Z/fMU1UzevISX9uvlGwcTlxKxtyGarPHneDXrzb39Cf40Vv7+y+4I777Y2db8x9nSNxuOkctKQoSRFxAh+JHvSmQVQofj0grPxJVHg4Ud2nDEENg2uwsjo3l0yNvxcR/ngc4qIGpsyIqZWFqzjXJIYNK7GYfakoG3Zqdf9fGDtlxajvrmlMYLWXZBCoIpGJZwxdHadvJr25avc5IFf2NG9u6U8+hIuKiE2mKnwL3LAS8Tg4rILMt1B50ln7Hjmm6v+UnTAvlGtb0nzwMUQGVc9Ita2LX8b2Z63UZnYx8S+/2Ji/3NUSwdqPj9AjE36k/NRZs0LxNGkhpllnPhbGyZWnNrzIZGpyvriGmVHk8BZRCaewdeqKulsD5n2HrpOWUd59CXG9/+c8shLRJUxUD9DppgajXrYCTPpe3h8XAUR2pafFq9YtSE0Adc+8Gl5Njegtti/+HbqsUDgYWpRr3gHIgHZZSvJLltJXK1QHnuZyYMvUB77JVF5BB9XZn1PZh0+k4aRDVpo61lJ+4lnRp1vOSWsTvDFx/5M/q43r0GxX+oyCnxsEThbQzU+plRpgjRtPafStvxUfOSplvZRHn+NqLSPuDqBdxGCYII0QbqddPYEMm0rCDJtUdhCWBn3//iTv7AfS5RXvwPhMUrg4apEZ8hEDOm2E8h0nDBdaFUOF6M6IhMSVifcj9s77QfIqynm8PUcEDAcTxA5xMxd5PGRxztFffLwcfJ6HPnIpgldxIPjo/ayoYKUZ+qN9UPAcQs50jySgrhUK6GrsC16jf5nvikTdZ6qmAuBySyMJJ0IfzxQquBEMGGGIC5z184OruHWWrpSaMyM4BFNuDePre3qIZvGKETHMneqxDbEiiWKK2zeeZtczVYUVWkUeW9I4NBWHKoSKJ+LS/xH2EqaZOjCHVvM4QAJWwjU8+8a866dt8kduZxapP4+b+5BREQRePR2eVXHWe+qfEksBGmsKl45ukSq4lB8kMGKpRJH3Lh3mHfu/KI82pvXIKl+izZ6HzKHnU43uN9xvb7TBhSM5RIAV0GVpIzPkkz8ozU/F9gUqAdVvucitv70dnkKoJH+bmEE1gJKLoeZ6mmcu0UvM8IWEfrEgq+C9ziSEUxT69bVizQ/1dgyFmtSyXoIP1DPrTtvk0GAWoLsl0J1CyCwhrwatqJTijzvBn2XwhWqXGZDlgH4CNThNYnckoz/TFMqb6yv6bqLr33YyqyCjIt4ReD7CF9/4hZ5fHpPwFKqbuEE1pDLqS0O4KeJ/LiuwLNR4X2qXGgsJ5ra34XV1UzNT1OkOqtEJskAuEyldWKTAr5IcjO850WBBxHuDZR/eeQ22T9FXG4XMq9O37FC4GwiAWZfxFmf0u5UhXMQzhc4V5VVKG8BusVi5VeK9Ko1gh0Rwn7gRWA3hp2iPJot89TQl2V8es0BtcWn0aOluLoSODvQ9G7FnrgLfT1FrLleu4xhuYFlonQ4aDFg1OMwTFoYFcu+wDC842YZe90blYOj4eOWhsDXCTivrUaOROj8vn/skdZgAo+QCm1FcrtmrZcDijMfKa5GKUz/f6OJJppoookmmmiiiSaaaKKJJpo4RvG/f5Y3yK60YDgAAAAASUVORK5CYII=',
};

/** Where a beginner goes to create the bot / app and obtain the credentials.
 *  Step-by-step text lives in i18n (`ravenChannels.setup.<name>`); the URL is a
 *  plain link. Channels that need no external signup (QR login, local bridge)
 *  have no URL. */
const CHANNEL_DOCS: Record<string, string> = {
	telegram: 'https://t.me/BotFather',
	discord: 'https://discord.com/developers/applications',
	feishu: 'https://open.feishu.cn/app',
	dingtalk: 'https://open-dev.dingtalk.com',
	slack: 'https://api.slack.com/apps',
	wecom: 'https://work.weixin.qq.com',
	qq: 'https://q.qq.com',
	matrix: 'https://element.io',
};

/** Channels that pair via a scan-a-QR login (no credentials typed). They expose
 *  a "Scan to log in" button that opens the QR dialog. */
const QR_LOGIN_CHANNELS = new Set(['weixin', 'whatsapp']);

/** Modal that polls the channel's pending login QR and shows it, closing itself
 *  (visually) once the channel reports connected. */
function ChannelQrDialog({
	channel,
	label,
	onClose,
}: {
	channel: string;
	label: string;
	onClose: () => void;
}) {
	const { t } = useTranslation();
	const [qr, setQr] = useState<string | null>(null);
	const [qrText, setQrText] = useState<string | null>(null);
	const [connected, setConnected] = useState(false);
	const [running, setRunning] = useState(true);

	useEffect(() => {
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout>;
		const poll = async () => {
			try {
				const r = await ravenConfigApi.channelQr(channel);
				if (cancelled) return;
				setQr(r.qr);
				setQrText(r.qr_text ?? null);
				setConnected(r.connected);
				setRunning(r.running);
				if (r.connected) return;
			} catch {
				// transient (e.g. gateway mid-restart) — keep polling
			}
			if (!cancelled) timer = setTimeout(poll, 2000);
		};
		void poll();
		return () => {
			cancelled = true;
			clearTimeout(timer);
		};
	}, [channel]);

	return (
		<div
			className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
			onClick={onClose}
		>
			<div
				className="w-full max-w-sm rounded-2xl border bg-card p-6 shadow-xl"
				onClick={(e) => e.stopPropagation()}
			>
				<div className="flex items-center justify-between">
					<h3 className="font-semibold">{t('ravenChannels.qrTitle', { name: label })}</h3>
					<button
						type="button"
						onClick={onClose}
						aria-label={t('common.close')}
						className="text-muted-foreground transition-colors hover:text-foreground"
					>
						<IconClose className="size-5" />
					</button>
				</div>
				<div className="mt-4 flex flex-col items-center gap-3">
					{connected ? (
						<div className="flex flex-col items-center gap-2 py-8 text-center">
							<IconCheck className="size-12 text-green-500" />
							<p className="text-sm">{t('ravenChannels.qrConnected', { name: label })}</p>
						</div>
					) : qr ? (
						<>
							<img
								src={qr}
								alt="login QR"
								className="size-56 rounded-lg bg-white p-2"
							/>
							<p className="text-center text-xs text-muted-foreground">
								{t('ravenChannels.qrScan', { name: label })}
							</p>
						</>
					) : qrText ? (
						<>
							<p className="text-center text-xs text-muted-foreground">
								{t('ravenChannels.qrTextFallback')}
							</p>
							<code className="w-full break-all rounded-md bg-muted p-2 text-center text-xs">
								{qrText}
							</code>
						</>
					) : (
						<div className="flex flex-col items-center gap-2 py-10 text-center text-muted-foreground">
							<Loader2 className="size-6 animate-spin" />
							<p className="text-xs">
								{running
									? t('ravenChannels.qrWaiting')
									: t('ravenChannels.qrNotRunning')}
							</p>
						</div>
					)}
				</div>
			</div>
		</div>
	);
}

/** `bot_token` -> `Bot token`: a readable label from a dotted/underscored path. */
function humanize(path: string): string {
	const leaf = path.split('.').pop() ?? path;
	const spaced = leaf.replace(/_/g, ' ');
	return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function ChannelCard({
	channel,
	onSaved,
	onRestartNeeded,
	onShowQr,
}: {
	channel: RavenChannel;
	onSaved: () => void;
	onRestartNeeded: () => void;
	onShowQr: (name: string, label: string) => void;
}) {
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const [showAdvanced, setShowAdvanced] = useState(false);
	const [edits, setEdits] = useState<Record<string, string>>({});
	const [saving, setSaving] = useState(false);

	const { label, Icon, tile } = metaFor(channel.name);
	const brand = BRAND_ICONS[channel.name];
	const brandImg = BRAND_IMAGES[channel.name];
	const blurb = t(`ravenChannels.blurbs.${channel.name}`, { defaultValue: '' });
	const setupText = t(`ravenChannels.setup.${channel.name}`, { defaultValue: '' });
	const docsUrl = CHANNEL_DOCS[channel.name];

	const fields = Object.entries(channel.specs).filter(([p]) => p !== 'enabled');
	const requiredFields = fields.filter(([, s]) => s.required);
	const optionalFields = fields.filter(([, s]) => !s.required);

	// Every required field must have a value before the channel can start; a
	// secret reads back as the ``****set****`` sentinel when present. Enabling an
	// unconfigured channel would just fail to connect on the next gateway start.
	const isConfigured = requiredFields.every(([p, spec]) => {
		const v = channel.config[p];
		if (spec.is_secret) return v != null && v !== '' && v !== '(empty)';
		return v != null && String(v).trim() !== '';
	});
	const isQrLogin = QR_LOGIN_CHANNELS.has(channel.name);

	const commit = async (patch: Record<string, string>, note: string) => {
		setSaving(true);
		try {
			const r = await ravenConfigApi.setChannel(channel.name, patch);
			if (r?.restart_required) onRestartNeeded();
			toast.success(note);
			setEdits({});
			onSaved();
		} catch (e) {
			toast.error(
				t('ravenChannels.saveFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setSaving(false);
		}
	};

	const toggle = () =>
		commit(
			{ enabled: String(!channel.enabled) },
			channel.enabled
				? t('ravenChannels.toggledDisabled', { name: label })
				: t('ravenChannels.toggledEnabled', { name: label }),
		);

	const save = () => {
		// Send only changed fields; never resend a redacted secret sentinel.
		const changed: Record<string, string> = {};
		for (const [p, spec] of fields) {
			const v = edits[p];
			if (v === undefined) continue;
			if (spec.is_secret) {
				if (v !== '') changed[p] = v;
			} else if (v !== String(channel.config[p] ?? '')) {
				changed[p] = v;
			}
		}
		if (Object.keys(changed).length === 0) {
			toast.info(t('ravenChannels.noChanges'));
			return;
		}
		void commit(changed, t('ravenChannels.savedChannel', { name: label }));
	};

	const renderField = ([p, spec]: (typeof fields)[number]) => (
		<div key={p} className="grid gap-1">
			<Label htmlFor={`${channel.name}-${p}`} className="text-xs font-medium">
				{humanize(p)}
				{spec.required && <span className="ml-0.5 text-destructive">*</span>}{' '}
				<span className="font-normal text-muted-foreground/70">({spec.type})</span>
			</Label>
			<Input
				id={`${channel.name}-${p}`}
				type={spec.is_secret ? 'password' : 'text'}
				value={edits[p] ?? (spec.is_secret ? '' : String(channel.config[p] ?? ''))}
				placeholder={spec.is_secret ? String(channel.config[p] ?? '') : spec.description || ''}
				onChange={(e) => setEdits({ ...edits, [p]: e.target.value })}
			/>
			{spec.description && (
				<span className="text-[11px] text-muted-foreground">{spec.description}</span>
			)}
		</div>
	);

	return (
		<div className="rounded-xl border bg-card p-4 transition-colors hover:border-foreground/15">
			<div className="flex items-start justify-between gap-3">
				<div className="flex min-w-0 items-start gap-3">
					<div
						className={cn(
							'flex size-10 shrink-0 items-center justify-center rounded-xl shadow-sm ring-1',
							brandImg
								? 'bg-white ring-black/10'
								: cn('bg-gradient-to-br text-white ring-black/5', tile),
						)}
					>
						{brandImg ? (
							<img src={brandImg} alt="" className="size-6" />
						) : brand ? (
							<BrandGlyph body={brand} className="size-5" />
						) : (
							<Icon className="size-5" />
						)}
					</div>
					<div className="min-w-0">
						<div className="flex items-center gap-2">
							<span className="font-semibold">{label}</span>
							<Badge
								variant={channel.enabled ? 'default' : 'outline'}
								className="text-[10px]"
							>
								{channel.enabled ? t('common.enabled') : t('common.disabled')}
							</Badge>
						</div>
						{blurb && <p className="mt-0.5 text-xs text-muted-foreground">{blurb}</p>}
					</div>
				</div>
				<div className="flex shrink-0 gap-1">
					{isQrLogin && channel.enabled && (
						<Button
							size="sm"
							variant="ghost"
							onClick={() => onShowQr(channel.name, label)}
						>
							{t('ravenChannels.qrButton')}
						</Button>
					)}
					<Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
						{open ? t('ravenChannels.hide') : t('ravenChannels.configure')}
					</Button>
					<Button
						size="sm"
						variant="outline"
						disabled={saving || (!channel.enabled && !isConfigured)}
						title={
							!channel.enabled && !isConfigured
								? t('ravenChannels.configureFirst')
								: undefined
						}
						onClick={toggle}
					>
						{channel.enabled ? t('ravenChannels.disable') : t('ravenChannels.enable')}
					</Button>
				</div>
			</div>

			{!channel.enabled && !isConfigured && (
				<button
					type="button"
					onClick={() => setOpen(true)}
					className="mt-2 text-left text-xs text-amber-600 hover:underline dark:text-amber-400"
				>
					{t('ravenChannels.configureFirst')}
				</button>
			)}
			{isQrLogin && channel.enabled && (
				<p className="mt-2 text-xs text-muted-foreground">{t('ravenChannels.qrSteps')}</p>
			)}

			{open && (
				<div className="mt-4 flex flex-col gap-3 border-t pt-4">
					{(setupText || docsUrl) && (
						<div className="rounded-lg border border-sky-500/30 bg-sky-500/[0.07] p-3 dark:bg-sky-500/10">
							<div className="text-xs font-semibold text-foreground">
								{t('ravenChannels.setupTitle')}
							</div>
							{setupText && (
								<p className="mt-1 text-xs leading-relaxed text-muted-foreground">
									{setupText}
								</p>
							)}
							{docsUrl && (
								<a
									href={docsUrl}
									target="_blank"
									rel="noreferrer noopener"
									className="mt-1.5 inline-flex items-center gap-1 text-xs font-medium text-sky-600 hover:underline dark:text-sky-300"
								>
									{t('ravenChannels.openDocs', { name: label })}
									<IconExternal className="size-3.5" />
								</a>
							)}
						</div>
					)}
					{requiredFields.length > 0 && (
						<>
							<div className="em-kicker">{t('ravenChannels.requiredSection')}</div>
							{requiredFields.map(renderField)}
						</>
					)}

					{optionalFields.length > 0 && (
						<>
							<button
								type="button"
								onClick={() => setShowAdvanced((v) => !v)}
								className="w-fit text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
							>
								{showAdvanced
									? t('ravenChannels.hideAdvanced')
									: t('ravenChannels.showAdvanced', {
											count: optionalFields.length,
										})}
							</button>
							{showAdvanced && optionalFields.map(renderField)}
						</>
					)}

					<div>
						<Button size="sm" disabled={saving} onClick={save}>
							{saving && <Loader2 className="size-4 animate-spin" />} {t('common.save')}
						</Button>
					</div>
				</div>
			)}
		</div>
	);
}

/** Configure Raven's IM channels. */
export function RavenChannelsPage() {
	const { t } = useTranslation();
	const [channels, setChannels] = useState<RavenChannel[]>([]);
	const [loading, setLoading] = useState(true);
	const [restartNeeded, setRestartNeeded] = useState(false);
	const [restarting, setRestarting] = useState(false);
	const [qrTarget, setQrTarget] = useState<{ name: string; label: string } | null>(null);

	const restartGateway = async () => {
		setRestarting(true);
		try {
			await ravenConfigApi.restartGateway();
		} catch {
			// The gateway drops the connection as it re-execs, so a transport error
			// here is expected — treat it as "restart in progress", not a failure.
		}
		toast.success(t('ravenChannels.restarting'));
		// It re-execs after a short delay and takes a few seconds to come back up.
		// Poll until it answers again rather than reloading on a fixed timer, or a
		// slow boot drops the page onto a backend that is still down.
		const deadline = Date.now() + 60000;
		const waitForGateway = async () => {
			while (Date.now() < deadline) {
				await new Promise((r) => setTimeout(r, 1500));
				try {
					await ravenConfigApi.listChannels({ silent: true });
					break;
				} catch {
					// still restarting
				}
			}
			window.location.reload();
		};
		void waitForGateway();
	};

	const load = useCallback(async () => {
		setLoading(true);
		try {
			const r = await ravenConfigApi.listChannels();
			setChannels(r.channels ?? []);
		} catch (e) {
			toast.error(
				t('ravenChannels.loadFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setLoading(false);
		}
	}, [t]);

	useEffect(() => {
		void load();
	}, [load]);

	// Enabled channels float to the top, but the order is settled once per load:
	// re-sorting on every save would make a card jump out from under the cursor
	// the moment it is toggled.
	const order = useMemo(
		() =>
			[...channels]
				.sort((a, b) => Number(b.enabled) - Number(a.enabled))
				.map((c) => c.name),
		// eslint-disable-next-line react-hooks/exhaustive-deps
		[channels.length, loading],
	);
	const sorted = useMemo(
		() => [...channels].sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name)),
		[channels, order],
	);
	const enabledCount = channels.filter((c) => c.enabled).length;

	return (
		<div className="h-full overflow-y-auto">
			<div className="mx-auto flex max-w-3xl flex-col gap-5 p-6">
				{/* Hero */}
				<div className="relative overflow-hidden rounded-2xl border bg-gradient-to-br from-sky-500/10 via-blue-500/[0.06] to-transparent p-6 dark:from-sky-500/15 dark:via-blue-500/10">
					<div className="pointer-events-none absolute -top-16 -right-16 size-48 rounded-full bg-gradient-to-br from-sky-500/20 to-blue-500/10 blur-3xl" />
					<div className="relative flex items-start gap-3.5">
						<div className="flex size-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500 to-blue-600 text-white shadow-md ring-1 ring-black/5">
							<IconChannels className="size-6" />
						</div>
						<div>
							<div className="em-kicker">{t('ravenChannels.kicker')}</div>
							<h1 className="text-2xl font-semibold tracking-tight">
								<span className="em-accent">{t('ravenChannels.title')}</span>
							</h1>
							<p className="mt-1 max-w-xl text-sm text-muted-foreground">
								{t('ravenChannels.subtitle')}
							</p>
							{!loading && (
								<p className="mt-1.5 text-xs text-muted-foreground/70">
									{t('ravenChannels.summary', {
										enabled: enabledCount,
										total: channels.length,
									})}
								</p>
							)}
						</div>
					</div>
				</div>

				{/* Restart-required banner (shown only when a save reports it) */}
				{restartNeeded && (
					<div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-700 dark:text-amber-300">
						<span>{t('ravenChannels.restartBanner')}</span>
						<Button
							size="sm"
							variant="outline"
							disabled={restarting}
							onClick={restartGateway}
							className="border-amber-500/50 text-amber-700 hover:bg-amber-500/10 dark:text-amber-300"
						>
							{restarting && <Loader2 className="size-4 animate-spin" />}{' '}
							{t('ravenChannels.restartNow')}
						</Button>
					</div>
				)}

				{loading ? (
					<div className="flex items-center gap-2 text-muted-foreground">
						<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
					</div>
				) : (
					<div className="flex flex-col gap-2.5">
						{sorted.map((c) => (
							<ChannelCard
								key={c.name}
								channel={c}
								onSaved={load}
								onRestartNeeded={() => setRestartNeeded(true)}
								onShowQr={(name, label) => setQrTarget({ name, label })}
							/>
						))}
					</div>
				)}
			</div>

			{qrTarget && (
				<ChannelQrDialog
					channel={qrTarget.name}
					label={qrTarget.label}
					onClose={() => setQrTarget(null)}
				/>
			)}
		</div>
	);
}
