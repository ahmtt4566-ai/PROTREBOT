export function coinBaseAsset(symbol: string): string;
export function coinAvatarColor(symbol: string): string;
type LogoEntry = {file: string; logoAsset?: string; note?: string};
type LogoOverride = {asset: string; legacyAsset?: string; note?: string};
export function coinLogoCandidates(symbol: string, catalog: Readonly<Record<string, LogoEntry>>, overrides: Readonly<Record<string, LogoOverride>>): Array<{file: string; source: 'manifest' | 'legacy'; logoAsset: string; note?: string}>;
