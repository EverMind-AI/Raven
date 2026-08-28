/// <reference types="vite/client" />
/// <reference types="vite-plugin-svgr/client" />
/// <reference types="unplugin-icons/types/react" />

interface ImportMetaEnv {
	readonly VITE_SERVER_URL?: string;
	readonly VITE_SERVICE_PORT?: string;
}

interface ImportMeta {
	readonly env: ImportMetaEnv;
}
