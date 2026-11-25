/** UI reflection of named service grants, not a substitute for server authorization.
 *  Workspace kinds mirror backend/app/modules/workspace/service.py.
 */
export const businessPermission = {
  sales: { read: "sales.read", write: "sales.draft" },
  purchasing: { read: "purchasing.read", write: "purchasing.write" },
  stocktake: { read: "stocktake.read", write: "stocktake.write" },
  materials: { read: "office.intake.read", write: "office.intake.write" },
  notifications: { read: "notifications.read", write: "notifications.ack" },
  knowledge: { review: "knowledge.review" },
  receiving: { read: "receiving.read", write: "receiving.write" },
} as const;

export function canUseQuoteWorkspace(capabilities: readonly string[]): boolean {
  return [businessPermission.sales.read, businessPermission.sales.write].some(grant => capabilities.includes(grant));
}

type ArtifactKind = "quote_draft" | "purchase_draft" | "stocktake_review" | "receiving_batch" | "work_item_sheet";
const artifactWriteGrants = {
  quote_draft: [businessPermission.sales.write],
  purchase_draft: [businessPermission.purchasing.write],
  stocktake_review: [businessPermission.stocktake.write],
  receiving_batch: [businessPermission.receiving.write],
  work_item_sheet: ["tasks.suggest", "tasks.manage"],
} as const satisfies Record<ArtifactKind, readonly string[]>;

export function canWriteArtifact(kind: string, has: (grant: string) => boolean): boolean {
  if (!Object.hasOwn(artifactWriteGrants, kind)) return false;
  return artifactWriteGrants[kind as ArtifactKind].some(has);
}

/** An action permission cannot by itself establish that Work history became unreadable. */
export function isWorkReadDenial(status: number, method = "GET"): boolean {
  return status === 401 || status === 403 && ["GET", "HEAD"].includes(method.toUpperCase());
}
