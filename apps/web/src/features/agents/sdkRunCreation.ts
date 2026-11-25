import { ApiError } from "../../lib/api/client";
import { authorizedRequest } from "../../lib/auth/session";
import type { AgentRunReceipt } from "./AgentChatPage";
import { checkedReceipt } from "./execution";

export type AgentCreationMode = "legacy" | "sdk-readonly";

/** Public build-time routing choice only. Host configuration and auth remain authoritative. */
export function resolveCreationMode(buildFlag: unknown): AgentCreationMode {
  return buildFlag === "1" ? "sdk-readonly" : "legacy";
}

export function canSendRunCommands(mode: AgentCreationMode): boolean {
  return mode === "legacy";
}

export interface PreparedRunSubmission {
  readonly mode: AgentCreationMode;
  readonly path: "/api/v1/agent-runs" | "/api/v1/agent-runs/sdk-readonly";
  readonly body: Readonly<{ text: string; run_id: string }>;
  readonly headers: Readonly<{ "Idempotency-Key": string }>;
}

const canonicalUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const preparedSubmissions = new WeakSet<PreparedRunSubmission>();
const attemptedSdkSubmissions = new WeakSet<PreparedRunSubmission>();

/** Capture mode, text and caller-created UUID once. Never normalize an already prepared request. */
export function prepareRunSubmission(
  mode: AgentCreationMode,
  body: { text: string; run_id?: string },
): PreparedRunSubmission {
  if (mode !== "legacy" && mode !== "sdk-readonly") throw new Error("Invalid creation mode");
  if (typeof body.text !== "string" || !body.text || typeof body.run_id !== "string") {
    throw new Error("A text and caller-created run ID are required");
  }
  if (mode === "sdk-readonly" && (
    body.text !== body.text.trim() || !canonicalUuid.test(body.run_id) ||
    Object.keys(body).length !== 2 || !("text" in body) || !("run_id" in body)
  )) throw new Error("SDK creation requires exact text and canonical UUID fields");
  const submission: PreparedRunSubmission = Object.freeze({
    mode,
    path: mode === "sdk-readonly" ? "/api/v1/agent-runs/sdk-readonly" : "/api/v1/agent-runs",
    body: Object.freeze({ text: body.text, run_id: body.run_id }),
    headers: Object.freeze({ "Idempotency-Key": body.run_id }),
  });
  preparedSubmissions.add(submission);
  return submission;
}

/** One request only. Exceptions do not authorize fallback, a new UUID, or another POST. */
export async function submitPreparedRun(
  submission: PreparedRunSubmission,
  baseUrl = "",
): Promise<AgentRunReceipt> {
  if (!preparedSubmissions.has(submission)) throw new Error("Submission was not prepared here");
  if (submission.mode === "sdk-readonly") {
    if (attemptedSdkSubmissions.has(submission)) throw new Error("Read the original Run; do not repeat SDK POST");
    attemptedSdkSubmissions.add(submission);
  }
  const next = await authorizedRequest<AgentRunReceipt>(submission.path, {
    method: "POST", baseUrl, headers: submission.headers, body: submission.body,
    timeoutMs: 120000,
  });
  return checkedReceipt(next, submission.body.run_id);
}

/** This is also the recovery path after 503/timeout/unknown. A 404 never becomes a create. */
export async function readExistingRun(runId: string, baseUrl = ""): Promise<AgentRunReceipt> {
  const next = await authorizedRequest<AgentRunReceipt>(
    `/api/v1/agent-runs/${encodeURIComponent(runId)}`, { method: "GET", baseUrl },
  );
  return checkedReceipt(next, runId);
}

/** Consumed by the actual Page error branch after its ticket/scope check. */
export function shouldHideReceiptOnError(mode: AgentCreationMode, error: unknown): boolean {
  return mode === "sdk-readonly" && error instanceof ApiError &&
    (error.status === 401 || error.status === 403);
}
