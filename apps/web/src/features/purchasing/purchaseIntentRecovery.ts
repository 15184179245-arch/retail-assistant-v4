import type { Session } from "../../lib/auth/session";

export type PurchaseIntentBody = {
  source_kind: "wechat_paste" | "delivery_note_text";
  supplier_name: string;
  original_text: string;
  consent_confirmed: true;
  line_descriptions: string[];
};
type Identity = Pick<Session, "organizationId" | "storeId" | "subjectId">;
type JournalStorage = Pick<Storage, "getItem" | "setItem">;
const digest = /^[0-9a-f]{64}$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const maxPending = 24;

export async function purchaseFingerprint(body: PurchaseIntentBody): Promise<string> {
  const data = new TextEncoder().encode(JSON.stringify([
    body.source_kind, body.supplier_name, body.original_text, body.consent_confirmed, body.line_descriptions,
  ]));
  const hash = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

export function purchasePendingJournal(storage: JournalStorage, actor: Identity) {
  const name = "v4-purchasing-unresolved:" + JSON.stringify([actor.organizationId, actor.storeId, actor.subjectId]);
  function read(): Record<string, string> {
    const raw = storage.getItem(name);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) ||
        Object.keys(parsed).length > maxPending || Object.entries(parsed).some(([key, value]) =>
          !digest.test(key) || typeof value !== "string" || !uuid.test(value))) {
      throw Error("Unrecognized purchasing receipt journal");
    }
    return parsed as Record<string, string>;
  }
  function write(value: Record<string, string>) {
    const data = JSON.stringify(value);
    storage.setItem(name, data);
    if (storage.getItem(name) !== data) throw Error("Purchasing receipt journal unavailable");
  }
  return {
    reserve(fingerprint: string) {
      const pending = read();
      if (pending[fingerprint]) return { key: pending[fingerprint], existing: true };
      if (Object.keys(pending).length >= maxPending) throw Error("Too many unresolved purchasing receipts");
      const key = crypto.randomUUID();
      write({ ...pending, [fingerprint]: key });
      return { key, existing: false };
    },
    resolve(fingerprint: string, key: string) {
      const pending = read();
      if (pending[fingerprint] !== key) return;
      delete pending[fingerprint];
      write(pending);
    },
  };
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/** The list can suggest a match, never establish which idempotent action committed. */
export function matchesPurchaseReceipt(value: unknown, body: PurchaseIntentBody, actor: Identity): boolean {
  const receipt = record(value), evidence = record(receipt?.evidence);
  if (!receipt || !evidence || typeof receipt.id !== "string" || !uuid.test(receipt.id) ||
      receipt.organization_id !== actor.organizationId || receipt.store_id !== actor.storeId ||
      receipt.creator_id !== actor.subjectId || receipt.status !== "awaiting_arrival" ||
      !Number.isSafeInteger(receipt.version) || (receipt.version as number) < 1 ||
      evidence.created_by !== actor.subjectId || evidence.consent_confirmed !== true ||
      evidence.source_kind !== body.source_kind || evidence.supplier_name !== body.supplier_name ||
      evidence.original_text !== body.original_text || !Array.isArray(receipt.lines)) return false;
  const descriptions = receipt.lines.map((line: unknown) => record(line)?.original_description);
  return descriptions.every(value => typeof value === "string") &&
    JSON.stringify(descriptions.sort()) === JSON.stringify([...body.line_descriptions].sort());
}
