import { authorizedRequest } from "../../lib/auth/session";

export interface ObservationSource {
  id: string;
  event_seq: number;
  session_id: string;
  kind: "observed" | "correction";
  supersedes_id: string | null;
  product_id: string | null;
  unknown_product_label: string | null;
  location_id: string | null;
  unknown_location_label: string | null;
  quantity: string;
  unit: string;
  media_id: string | null;
  observed_by: string;
  observed_at: string;
  recorded_at: string;
}

export async function readStocktakeObservationPage(
  sessionId: string, after: number, limit: number, signal?: AbortSignal,
): Promise<ObservationSource[]> {
  if (!sessionId || !Number.isSafeInteger(after) || after < 0 || !Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw Error("invalid_stocktake_cursor");
  }
  const path = "/api/v1/stocktake/sessions/" + encodeURIComponent(sessionId) +
    "/observations?after_seq=" + after + "&limit=" + limit;
  const rows = await authorizedRequest<ObservationSource[]>(path, { signal, cache: "no-store" });
  if (!Array.isArray(rows) || rows.length > limit) throw Error("invalid_stocktake_page");
  let previous = after;
  for (const item of rows) {
    if (!item || typeof item.id !== "string" || !item.id || !Number.isSafeInteger(item.event_seq) ||
        item.event_seq <= previous || item.session_id !== sessionId) {
      throw Error("invalid_stocktake_cursor");
    }
    previous = item.event_seq;
  }
  return rows;
}

/** Seek directly to one immutable observation; do not scan or trust a URL from the snapshot. */
export async function readStocktakeObservation(
  sessionId: string, observationId: string, sequence: number, signal: AbortSignal,
): Promise<ObservationSource | null> {
  if (!observationId || !Number.isSafeInteger(sequence) || sequence < 1) throw Error("invalid_stocktake_evidence");
  const [row] = await readStocktakeObservationPage(sessionId, sequence - 1, 1, signal);
  if (!row || row.id !== observationId || row.event_seq !== sequence) return null;
  if ((row.kind !== "observed" && row.kind !== "correction") ||
      ![row.quantity, row.unit, row.observed_at, row.recorded_at].every(value => typeof value === "string")) {
    throw Error("invalid_stocktake_evidence");
  }
  return row;
}
