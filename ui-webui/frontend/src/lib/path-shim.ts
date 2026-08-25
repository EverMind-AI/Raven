// Browser-safe replacement for Node's `path` module (POSIX semantics).
//
// Aliased to `path` in `vite.config.ts` so browser deps that do
// `require('path')` — notably `mime-types`, whose module init reads
// `require('path').extname` — resolve to this instead of Vite's externalized
// stub. Reading a member off that stub logs the runtime warning:
//   Module "path" has been externalized for browser compatibility.
//   Cannot access "path.extname" in client code.
// Providing a real implementation removes the warning and keeps `mime-types`
// fully functional in the browser (both `extension()` and `lookup()`).
//
// `extname` follows Node's exact algorithm; the rest cover the common surface
// so this is a safe drop-in for any client dep that reaches for `path`.

const CHAR_DOT = 46; /* . */
const CHAR_FORWARD_SLASH = 47; /* / */

function assertPath(path: unknown): asserts path is string {
	if (typeof path !== 'string') {
		throw new TypeError('Path must be a string. Received ' + JSON.stringify(path));
	}
}

// Resolve `.` and `..` segments. Ported from Node's internal posix helper.
function normalizeStringPosix(path: string, allowAboveRoot: boolean): string {
	let res = '';
	let lastSegmentLength = 0;
	let lastSlash = -1;
	let dots = 0;
	let code = 0;
	for (let i = 0; i <= path.length; ++i) {
		if (i < path.length) code = path.charCodeAt(i);
		else if (code === CHAR_FORWARD_SLASH) break;
		else code = CHAR_FORWARD_SLASH;

		if (code === CHAR_FORWARD_SLASH) {
			if (lastSlash === i - 1 || dots === 1) {
				// noop
			} else if (lastSlash !== i - 1 && dots === 2) {
				if (
					res.length < 2 ||
					lastSegmentLength !== 2 ||
					res.charCodeAt(res.length - 1) !== CHAR_DOT ||
					res.charCodeAt(res.length - 2) !== CHAR_DOT
				) {
					if (res.length > 2) {
						const lastSlashIndex = res.lastIndexOf('/');
						if (lastSlashIndex !== res.length - 1) {
							if (lastSlashIndex === -1) {
								res = '';
								lastSegmentLength = 0;
							} else {
								res = res.slice(0, lastSlashIndex);
								lastSegmentLength = res.length - 1 - res.lastIndexOf('/');
							}
							lastSlash = i;
							dots = 0;
							continue;
						}
					} else if (res.length === 2 || res.length === 1) {
						res = '';
						lastSegmentLength = 0;
						lastSlash = i;
						dots = 0;
						continue;
					}
				}
				if (allowAboveRoot) {
					if (res.length > 0) res += '/..';
					else res = '..';
					lastSegmentLength = 2;
				}
			} else {
				if (res.length > 0) res += '/' + path.slice(lastSlash + 1, i);
				else res = path.slice(lastSlash + 1, i);
				lastSegmentLength = i - lastSlash - 1;
			}
			lastSlash = i;
			dots = 0;
		} else if (code === CHAR_DOT && dots !== -1) {
			++dots;
		} else {
			dots = -1;
		}
	}
	return res;
}

export function extname(path: string): string {
	assertPath(path);
	let startDot = -1;
	let startPart = 0;
	let end = -1;
	let matchedSlash = true;
	// Track the state of characters (if any) we see before the first dot and
	// after any path separator we find.
	let preDotState = 0;
	for (let i = path.length - 1; i >= 0; --i) {
		const code = path.charCodeAt(i);
		if (code === CHAR_FORWARD_SLASH) {
			// If we reached a path separator that was not part of a set of path
			// separators at the end of the string, stop now.
			if (!matchedSlash) {
				startPart = i + 1;
				break;
			}
			continue;
		}
		if (end === -1) {
			// We saw the first non-path separator, mark this as the end of our
			// extension.
			matchedSlash = false;
			end = i + 1;
		}
		if (code === CHAR_DOT) {
			// If this is our first dot, mark it as the start of our extension.
			if (startDot === -1) startDot = i;
			else if (preDotState !== 1) preDotState = 1;
		} else if (startDot !== -1) {
			// We saw a non-dot and non-path separator before our dot, so we
			// should have a good chance at having a non-empty extension.
			preDotState = -1;
		}
	}

	if (
		startDot === -1 ||
		end === -1 ||
		// We saw a non-dot character immediately before the dot.
		preDotState === 0 ||
		// The (right-most) trimmed path component is exactly `..`.
		(preDotState === 1 && startDot === end - 1 && startDot === startPart + 1)
	) {
		return '';
	}
	return path.slice(startDot, end);
}

export function basename(path: string, ext?: string): string {
	assertPath(path);
	if (ext !== undefined && typeof ext !== 'string') {
		throw new TypeError('"ext" argument must be a string');
	}
	let start = 0;
	let end = -1;
	let matchedSlash = true;

	if (ext !== undefined && ext.length > 0 && ext.length <= path.length) {
		if (ext === path) return '';
		let extIdx = ext.length - 1;
		let firstNonSlashEnd = -1;
		for (let i = path.length - 1; i >= 0; --i) {
			const code = path.charCodeAt(i);
			if (code === CHAR_FORWARD_SLASH) {
				if (!matchedSlash) {
					start = i + 1;
					break;
				}
			} else {
				if (firstNonSlashEnd === -1) {
					matchedSlash = false;
					firstNonSlashEnd = i + 1;
				}
				if (extIdx >= 0) {
					if (code === ext.charCodeAt(extIdx)) {
						if (--extIdx === -1) end = i;
					} else {
						extIdx = -1;
						end = firstNonSlashEnd;
					}
				}
			}
		}

		if (start === end) end = firstNonSlashEnd;
		else if (end === -1) end = path.length;
		return path.slice(start, end);
	}
	for (let i = path.length - 1; i >= 0; --i) {
		if (path.charCodeAt(i) === CHAR_FORWARD_SLASH) {
			if (!matchedSlash) {
				start = i + 1;
				break;
			}
		} else if (end === -1) {
			matchedSlash = false;
			end = i + 1;
		}
	}

	if (end === -1) return '';
	return path.slice(start, end);
}

export function dirname(path: string): string {
	assertPath(path);
	if (path.length === 0) return '.';
	let end = -1;
	let matchedSlash = true;
	for (let i = path.length - 1; i > 0; --i) {
		if (path.charCodeAt(i) === CHAR_FORWARD_SLASH) {
			if (!matchedSlash) {
				end = i;
				break;
			}
		} else {
			matchedSlash = false;
		}
	}

	if (end === -1) return path.charCodeAt(0) === CHAR_FORWARD_SLASH ? '/' : '.';
	if (end === 0 && path.charCodeAt(0) === CHAR_FORWARD_SLASH) return '/';
	return path.slice(0, end);
}

export function isAbsolute(path: string): boolean {
	assertPath(path);
	return path.length > 0 && path.charCodeAt(0) === CHAR_FORWARD_SLASH;
}

export function normalize(path: string): string {
	assertPath(path);
	if (path.length === 0) return '.';
	const isAbs = path.charCodeAt(0) === CHAR_FORWARD_SLASH;
	const trailingSeparator = path.charCodeAt(path.length - 1) === CHAR_FORWARD_SLASH;

	let normalized = normalizeStringPosix(path, !isAbs);
	if (normalized.length === 0 && !isAbs) normalized = '.';
	if (normalized.length > 0 && trailingSeparator) normalized += '/';

	if (isAbs) return '/' + normalized;
	return normalized;
}

export function join(...paths: string[]): string {
	if (paths.length === 0) return '.';
	let joined: string | undefined;
	for (let i = 0; i < paths.length; ++i) {
		const arg = paths[i];
		assertPath(arg);
		if (arg.length > 0) {
			if (joined === undefined) joined = arg;
			else joined += '/' + arg;
		}
	}
	if (joined === undefined) return '.';
	return normalize(joined);
}

export function resolve(...paths: string[]): string {
	let resolvedPath = '';
	let resolvedAbsolute = false;
	for (let i = paths.length - 1; i >= 0 && !resolvedAbsolute; --i) {
		const path = paths[i];
		assertPath(path);
		if (path.length === 0) continue;
		resolvedPath = path + '/' + resolvedPath;
		resolvedAbsolute = path.charCodeAt(0) === CHAR_FORWARD_SLASH;
	}
	// The browser has no cwd; treat unresolved paths as rooted at `/`.
	resolvedPath = normalizeStringPosix(resolvedPath, !resolvedAbsolute);
	if (resolvedAbsolute) return '/' + resolvedPath;
	return resolvedPath.length > 0 ? '/' + resolvedPath : '/';
}

export const sep = '/';
export const delimiter = ':';

const posix = {
	extname,
	basename,
	dirname,
	isAbsolute,
	normalize,
	join,
	resolve,
	sep,
	delimiter,
};

export default posix;
