const SITES = new Set(["7-1", "7-2", "8-1", "8-2", "9-1", "9-2", "_all"]);
const COLLECTIONS = new Set(["reports", "crops", "reviewed", "people"]);

export function ok(site: string, collection: string): boolean {
  return SITES.has(site) && COLLECTIONS.has(collection);
}
