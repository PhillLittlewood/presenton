export function isCommunityEnabled(value?: string | null): boolean {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return true;
  return !["0", "false", "no", "off"].includes(normalized);
}
