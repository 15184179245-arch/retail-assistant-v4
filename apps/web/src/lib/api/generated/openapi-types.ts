/* eslint-disable */
/**
 * Generated from contracts/openapi.json
 * Original projection: 342 schemas / 175 paths.
 * Incrementally aligned to 389 schemas / 203 paths, source SHA-256:
 * 46b4ef2d2b474ecad98d6924f82b446f0e0c450b23b3f7f4385ac8db0e438d5b
 * ERP dispatch/reconcile declare 503; unwired Office review writes declare 409.
 * DTO/path shapes unchanged.
 * Updated 4 existing schema shapes; added 47 schemas and 28 path summaries.
 * Compile-time shape projection only, not JSON Schema runtime validation.
 */

export interface StandardApiEnvelope<T = unknown> {
  success: boolean;
  data: T | null;
  code: string;
  error: string | null;
  user_message: string;
  hint: string | null;
}

export type ApiOperationPath = keyof OpenApiPaths;

export type AgentCancelResolution = "not_requested" | "requested" | "cancelled" | "too_late" | "unknown";

export interface AgentCostEnvelope {
  code: string;
  data: AgentCostPage;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface AgentCostItem {
  call_key: string;
  created_at: string;
  currency: string;
  error_code: string | null;
  estimated_amount: string | null;
  id: string;
  model: string;
  price_version: string;
  provider_key: string;
  requester_id: string;
  reserved_amount: string;
  run_id: string;
  settled_at: string | null;
  status: "reserved" | "settled" | "unknown" | "released" | "reconciliation_required";
  step_id: string;
}

export interface AgentCostPage {
  items: AgentCostItem[];
  next_cursor: string | null;
}

export type AgentEffectStatus = "none" | "local_task_committed" | "external_unknown";

export type AgentExecutionPhase = "queued" | "claimed" | "model_inflight" | "waiting_for_human" | "tool_inflight" | "reconciling" | "terminal";

export interface AgentExecutionView {
  cancel_requested: boolean;
  cancel_resolution: AgentCancelResolution;
  effect_status: AgentEffectStatus;
  phase: AgentExecutionPhase;
  protocol_version: 1;
}

export interface AgentReceiptEnvelope {
  code: string;
  data: AgentRunReceipt | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface AgentRequest {
  run_id?: string | null;
  text: string;
}

export interface AgentRunReceipt {
  answer?: (string | null);
  answer_status?: "available" | "not_recorded" | "unavailable";
  answer_unavailable_reason?: ("answer_or_source_unavailable" | null);
  citations?: (AnswerCitation)[];
  error_code?: (string | null);
  execution?: (AgentExecutionView | null);
  pending_action?: (Record<string, unknown> | null);
  public_trace?: PublicTrace;
  replayed?: boolean;
  run_id: string;
  state: RunState;
  steps?: (Record<string, unknown>)[];
  task_id?: (string | null);
}

export interface AllocationLineProposal {
  allocated_amount: string;
  document_ref: string;
  line_id: string;
  note?: string | null;
}

export interface AnswerCitation {
  kind: "knowledge" | "catalog" | "memory" | "guanjia_observation" | "guanjia_category" | "research" | "product_image_result" | "history_image_result";
  label: string;
  object_id: string;
  observed_at: (string | null);
  source_ref: (string | null);
  version: string;
  version_ref: string;
}

export interface ApiEnvelope {
  code: string;
  data: unknown | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ApprovalCancellation {
  reason: string;
}

export interface ApprovalCreate {
  action_key: string;
  expires_at: string;
  reason: string;
  subject_id: string;
  subject_type: string;
}

export interface ApprovalDecision {
  decision: "approved" | "rejected";
  reason: string;
}

export interface ApprovalEnvelope {
  code: string;
  data: ApprovalResponse | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ApprovalListEnvelope {
  code: string;
  data: ApprovalPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ApprovalPage {
  items: ApprovalResponse[];
  next_cursor: string | null;
}

export interface ApprovalProof {
  approval_id: string;
  kind: "approval";
}

export interface ApprovalResponse {
  action_key: string;
  cancellation_reason: string | null;
  cancelled_at: string | null;
  cancelled_by: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
  expired_at: string | null;
  expired_by: string | null;
  expires_at: string;
  id: string;
  organization_id: string;
  reason: string;
  requested_at: string;
  requester_id: string;
  state: "requested" | "approved" | "rejected" | "cancelled" | "expired";
  store_id: string;
  subject_id: string;
  subject_type: string;
}

export interface ArtifactCreateRequest {
  actor_type?: "human" | "agent";
  idempotency_key?: string | null;
  kind: "quote_draft" | "purchase_draft" | "stocktake_review" | "receiving_batch" | "work_item_sheet";
  payload?: Record<string, unknown>;
  source_refs?: Record<string, unknown>[];
  thread_id: string;
  title: string;
}

export interface ArtifactDetailData {
  artifact: ArtifactRecord;
  ops?: ArtifactOpRecord[];
}

export interface ArtifactDetailEnvelope {
  code: string;
  data?: ArtifactDetailData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ArtifactListData {
  items: ArtifactRecord[];
}

export interface ArtifactListEnvelope {
  code: string;
  data?: ArtifactListData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ArtifactLockRequest {
  base_version: number;
  fields: string[];
}

export interface ArtifactMutationData {
  artifact: ArtifactRecord;
  idempotent_replay?: boolean;
  message?: string | null;
  op: ArtifactOpRecord;
  pending_suggestions_added?: PendingSuggestionRecord[];
  skipped_fields?: string[];
}

export interface ArtifactMutationEnvelope {
  code: string;
  data?: ArtifactMutationData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ArtifactOpRecord {
  actor_subject_id: string;
  actor_type: "human" | "agent";
  applied_fields: string[];
  artifact_id: string;
  created_at: string;
  id: string;
  idempotency_key?: string | null;
  op_type: "create" | "patch" | "undo" | "lock" | "unlock" | "accept_suggestion" | "ignore_suggestion" | "submit";
  patch: Record<string, unknown>;
  skipped_fields: string[];
  undo_of_op_id?: string | null;
  version_after: number;
  version_before: number;
}

export interface ArtifactPatchRequest {
  actor_type?: "human" | "agent";
  base_version: number;
  idempotency_key?: string | null;
  patch: Record<string, unknown>;
  reason?: string | null;
  title?: string | null;
}

export interface ArtifactRecord {
  created_at: string;
  id: string;
  kind: "quote_draft" | "purchase_draft" | "stocktake_review" | "receiving_batch" | "work_item_sheet";
  locked_fields: string[];
  organization_id: string;
  owner_subject_id: string;
  payload: Record<string, unknown>;
  pending_suggestions: PendingSuggestionRecord[];
  source_refs: Record<string, unknown>[];
  status: "draft" | "submitted" | "archived";
  store_id: string;
  submitted_receipt_ref?: string | null;
  thread_id: string;
  title: string;
  updated_at: string;
  version: number;
}

export interface ArtifactSubmitRequest {
  base_version: number;
  idempotency_key?: string | null;
  note?: string | null;
}

export interface ArtifactSuggestionDecisionRequest {
  base_version: number;
  field?: string | null;
}

export interface ArtifactUndoRequest {
  base_version: number;
  idempotency_key?: string | null;
  target_op_id: string;
}

export interface ArtifactUnlockRequest {
  base_version: number;
  field?: string | null;
  fields?: string[] | null;
}

export interface AssertionCreate {
  idempotency_key: string;
  kind: string;
  product_id: string;
}

export interface AssertionEnvelope {
  code: string;
  data: AssertionOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface AssertionListEnvelope {
  code: string;
  data: Record<string, unknown> | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface AssertionOut {
  asserted_at: string;
  asserted_by: string;
  id: string;
  kind: string;
  location_id: string;
  product_id: string;
}

export interface AssetSnapshot {
  byte_size: number;
  category: "category_image" | "inventory_photo";
  content_type: "image/jpeg" | "image/png" | "image/webp";
  created_at: string;
  created_by: string;
  id: string;
  sha256: string;
}

export interface AuditEventItem {
  action: string;
  actor_id: string;
  created_at: string;
  id: string;
  occurred_at: string;
  organization_id: string;
  store_id: string;
  target_id: string;
  target_type: string;
}

export interface AuditEventPage {
  items: AuditEventItem[];
  next_cursor: string | null;
}

export interface AuditEventsEnvelope {
  code: string;
  data: AuditEventPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface BatchReviewDecide {
  items: ReviewItemDecide[];
}

export interface BindAction {
  asset: AssetSnapshot;
  operation: "image_bind";
  upload_proof: UploadProof;
}

export interface BlindSessionEnvelope {
  code: string;
  data: BlindSessionView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface BlindSessionView {
  blind: boolean;
  book_difference: null;
  book_quantity: null;
  coverage_kind: string;
  coverage_location_id: string | null;
  coverage_note: string;
  full_store_coverage: boolean;
  inventory_accuracy: null;
  message: string;
  opened_at: string;
  session_id: string;
  session_version: number;
}

export interface BlockPinData {
  artifact: ArtifactRecord;
  op: ArtifactOpRecord;
  resolved_block: ResolvedBlockRecord;
}

export interface BlockPinEnvelope {
  code: string;
  data?: BlockPinData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface BlockPinRequest {
  block: Record<string, unknown>;
  idempotency_key?: string | null;
  kind?: "quote_draft" | "purchase_draft" | "stocktake_review" | "receiving_batch" | "work_item_sheet" | null;
  thread_id: string;
  title?: string | null;
}

export interface BlockProvenance {
  source: string;
  stale_refs?: Record<string, unknown>[];
  status_kind: "development" | "production" | "suggestion" | "fact" | "pending_sync" | "settled" | "verified" | "unverified" | "waiting" | "error";
  stripped_fields?: string[];
  synced_at?: string | null;
  verified_by?: string | null;
}

export interface BlockResolveData {
  block: ResolvedBlockRecord;
  blocks: ResolvedBlockRecord[];
}

export interface BlockResolveEnvelope {
  code: string;
  data?: BlockResolveData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface BlockResolveRequest {
  block?: Record<string, unknown> | null;
  blocks?: Record<string, unknown>[] | null;
  props?: Record<string, unknown> | null;
  refs?: Record<string, unknown>[] | null;
  thread_id?: string | null;
  type?: string | null;
  version?: string | null;
}

export interface CalendarRuleInput {
  cadence: "daily" | "weekly";
  fold_policy: "first" | "second";
  gap_policy: "skip";
  local_time: string;
  timezone_name: string;
  weekdays: number[];
  window_seconds: number;
}

export interface CandidateOut {
  confidence: string;
  field_id: string;
  field_key: "product" | "quantity" | "unit" | "price" | "supplier";
  flags: string[];
  location?: Record<string, unknown> | null;
  normalized_value: string | null;
  raw_text: string;
  span_id: string;
  state: "needs_review";
}

export interface CaseCommandInput {
  expected_resolution_digest?: string | null;
  expected_source_digest: string;
  expected_version: number;
  operation: "claim" | "takeover" | "propose_transfer" | "accept_transfer" | "cancel_transfer" | "resolve" | "reopen";
  reason: string;
  to_owner_id?: string | null;
}

export interface CaseCommandReceipt {
  actor_id: string;
  case_id: string;
  case_version: number;
  created_at: string;
  from_owner_id?: string | null;
  id: string;
  operation: "create" | "claim" | "takeover" | "propose_transfer" | "accept_transfer" | "cancel_transfer" | "resolve" | "reopen";
  reason_digest: string;
  references: ReceiptReferences;
  request_digest: string;
  resolution_digest?: string | null;
  source_digest: string;
  to_owner_id?: string | null;
}

export interface CaseCommandStatus {
  case: CaseView | null;
  effect?: "case_tracking_only";
  mode?: "human_supervised";
  status: "found" | "not_found";
}

export interface CaseCommandStatusEnvelope {
  code: string;
  data: CaseCommandStatus | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CaseCreateInput {
  expected_source_digest: string;
  expected_source_version: number | null;
  kind: "shortage" | "overage" | "wrong_item" | "identity_ambiguous" | "partial" | "unknown_write" | "overdue" | "price_change" | "workflow_attention";
  reason: string;
  source_id: string;
  source_type: "receiving_discrepancy" | "erp_sandbox" | "erp_intent" | "sales_handoff" | "workflow" | "task";
}

export interface CaseEnvelope {
  code: string;
  data: CaseView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CaseListEnvelope {
  code: string;
  data: CaseListPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CaseListPage {
  effect?: "case_tracking_only";
  items: CaseSummary[];
  mode?: "human_supervised";
  next_cursor?: string | null;
}

export interface CaseReceiptPage {
  case_id: string;
  case_version: number;
  effect?: "case_tracking_only";
  mode?: "human_supervised";
  next_after_version?: number | null;
  receipts: CaseCommandReceipt[];
}

export interface CaseReceiptPageEnvelope {
  code: string;
  data: CaseReceiptPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CaseSummary {
  created_at: string;
  created_by: string;
  effect?: "case_tracking_only";
  id: string;
  initial_source_digest: string;
  kind: "shortage" | "overage" | "wrong_item" | "identity_ambiguous" | "partial" | "unknown_write" | "overdue" | "price_change" | "workflow_attention";
  mode?: "human_supervised";
  owner_available: boolean;
  owner_id?: string | null;
  pending_owner_available?: boolean | null;
  pending_owner_id?: string | null;
  resolution_digest?: string | null;
  resolved_at?: string | null;
  source_id: string;
  source_type: "receiving_discrepancy" | "erp_sandbox" | "erp_intent" | "sales_handoff" | "workflow" | "task";
  source_version: number | null;
  state: "open" | "owned" | "transfer_pending" | "resolved";
  updated_at: string;
  version: number;
}

export interface CaseView {
  blocking_code?: "SOURCE_UNAVAILABLE" | null;
  command_receipt?: CaseCommandReceipt | null;
  created_at: string;
  created_by: string;
  effect?: "case_tracking_only";
  id: string;
  initial_source_digest: string;
  kind: "shortage" | "overage" | "wrong_item" | "identity_ambiguous" | "partial" | "unknown_write" | "overdue" | "price_change" | "workflow_attention";
  mode?: "human_supervised";
  owner_available: boolean;
  owner_id?: string | null;
  pending_owner_available?: boolean | null;
  pending_owner_id?: string | null;
  replayed?: boolean;
  resolution_digest?: string | null;
  resolved_at?: string | null;
  source: ExceptionSource | null;
  source_changed: boolean;
  source_id: string;
  source_type: "receiving_discrepancy" | "erp_sandbox" | "erp_intent" | "sales_handoff" | "workflow" | "task";
  source_version: number | null;
  state: "open" | "owned" | "transfer_pending" | "resolved";
  updated_at: string;
  version: number;
}

export interface CatalogAttemptView {
  applied_at: string | null;
  created_at: string;
  delivery_ref: string | null;
  dispatched_at: string | null;
  executor_id: string;
  external_ref: string | null;
  id: string;
  input_digest: string;
  intent_id: string;
  proof: CatalogResult | null;
  receipt_digest: string | null;
  result_remote_version: number | null;
  state: "prepared" | "dispatched" | "unknown" | "sandbox_verified" | "failed" | "cancelled";
  updated_at: string;
  version: number;
}

export interface CatalogCacheEnvelope {
  code: string;
  data: CatalogCachePage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogCacheItem {
  active: boolean;
  barcode: string | null;
  barcode_flags: string[];
  brand: string | null;
  category: string | null;
  external_code: string;
  model: string | null;
  name: string | null;
  price_captured_at: string;
  price_source_version: string;
  product_id: string;
  sell_price: number | null;
  snapshot_sha256: string;
  source_system: string;
  source_updated_at: string | null;
  source_version: string;
  spec: string | null;
  unit: string | null;
  version: number;
}

export interface CatalogCachePage {
  catalog_version: string;
  changed?: boolean;
  generated_at: string;
  has_more: boolean;
  items: CatalogCacheItem[];
  next_cursor: string | null;
  reset_required?: boolean;
}

export interface CatalogCancelInput {
  expected_attempt_version: number;
  input_digest: string;
  reason: string;
}

export interface CatalogCommandEnvelope {
  code: string;
  data: CatalogCommandView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogCommandStatus {
  command: CatalogCommandView | null;
  status: "found" | "not_found";
}

export interface CatalogCommandStatusEnvelope {
  code: string;
  data: CatalogCommandStatus | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogCommandView {
  action_digest: string;
  attempt: CatalogAttemptView | null;
  catalog_state: "not_verified";
  context_digest: string;
  created_at: string;
  effect: "local_sandbox_only";
  id: string;
  mode: "synthetic";
  operation: "text_update" | "image_upload" | "image_bind";
  product_id: string;
  proposal_digest: string;
  replayed: boolean;
  request: CatalogERPCommandOut;
  source_digest: string;
  source_snapshot: CatalogContext;
  state: "prepared" | "dispatched" | "unknown" | "sandbox_verified" | "failed" | "cancelled" | "pending" | "approved" | "rejected" | "staged" | "manual_rejected";
  target_version: number;
}

export interface CatalogConflictListEnvelope {
  code: string;
  data: unknown | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogContext {
  action: TextAction | UploadAction | BindAction;
  source: CatalogSource;
}

export interface CatalogContextEnvelope {
  code: string;
  data: CatalogContextView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogContextView {
  action_digest: string;
  context: CatalogContext;
  context_digest: string;
  source_digest: string;
}

export interface CatalogDecideInput {
  decision: "approved" | "rejected";
  expires_at: string;
  reason: string;
  request_digest: string;
}

export interface CatalogERPApprovalReceipt {
  approved_by: string;
  decision: "approved" | "rejected";
  expires_at: string;
  id: string;
  reason: string;
  request_digest: string;
}

export interface CatalogERPCommandOut {
  approval: CatalogERPApprovalReceipt | null;
  expires_at: string;
  id: string;
  replayed: boolean;
  request: CatalogERPRequestDetails;
  request_digest: string;
  state: "pending" | "approved" | "rejected" | "staged" | "unknown" | "manual_rejected";
  target_digest: string;
}

export interface CatalogERPParameters {
  action_digest: string;
  context_digest: string;
  operation: "text_update" | "image_upload" | "image_bind";
}

export interface CatalogERPRequestDetails {
  action: "catalog_maintenance_proposal";
  business_parameters: CatalogERPParameters;
  requested_by: string;
  source_sha256: string;
  target_id: string;
  target_version: number;
}

export interface CatalogObservationPage {
  attempt_id: string | null;
  command_id: string;
  items: CatalogObservationView[];
  next_cursor: string | null;
}

export interface CatalogObservationPageEnvelope {
  code: string;
  data: CatalogObservationPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogObservationView {
  actor_id: string;
  attempt_id: string;
  created_at: string;
  evidence_digest: string;
  id: string;
  kind: "dispatch_intent" | "ack" | "readback" | "network_error" | "cancelled";
}

export interface CatalogPreviewInput {
  action: TextUpdateInput | ImageUploadInput | ImageBindInput;
  product_id: string;
}

export interface CatalogProductEnvelope {
  code: string;
  data: CatalogProductOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogProductListEnvelope {
  code: string;
  data: unknown | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface CatalogProductOut {
  active: boolean;
  external_code: string;
  id: string;
  organization_id: string;
  snapshot?: CatalogSnapshotOut | null;
  source_system: string;
  source_updated_at: string | null;
  source_version: string;
  store_id: string;
  version: number;
}

export interface CatalogProposeInput {
  action: TextUpdateInput | ImageUploadInput | ImageBindInput;
  expected_context_digest: string;
  expected_target_version: number;
  expires_at: string;
  product_id: string;
  reason: string;
}

export interface CatalogResult {
  action_digest: string;
  asset_ref: string | null;
  asset_sha256: string | null;
  error_code: string | null;
  field_name: "display_label" | "specification_note" | null;
  field_value: string | null;
  operation: "text_update" | "image_upload" | "image_bind";
  outcome: "accepted" | "rejected";
  remote_version: number;
}

export interface CatalogSnapshotOut {
  captured_at: string;
  id: string;
  payload: Record<string, unknown>;
  payload_sha256: string;
  source_updated_at: string | null;
  source_version: string;
}

export interface CatalogSource {
  effect: "local_sandbox_only";
  extension_digest: string | null;
  extension_id: string | null;
  extension_version: number | null;
  external_code: string;
  organization_id: string;
  original_fields: TextFields;
  product_id: string;
  product_version: number;
  real_erp_mapping: "unverified";
  sandbox_fields: TextFields;
  sandbox_image_ref: string | null;
  sandbox_remote_version: number;
  snapshot_captured_at: string;
  snapshot_id: string;
  snapshot_payload_digest: string;
  snapshot_sha256: string;
  snapshot_source_updated_at: string | null;
  source_system: string;
  source_updated_at: string | null;
  source_version: string;
  store_id: string;
  target_version: number;
}

export interface CatalogStageInput {
  approval_id: string;
  expected_target_version: number;
  request_digest: string;
}

export interface ConfirmationDecision {
  decision: "approve" | "reject";
  input_digest: string;
  reason: string;
  run_version: number;
  step_version: number;
}

export interface ConversionReceiptEnvelope {
  code: string;
  data: ConversionReceiptView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ConversionReceiptView {
  effective_from: string;
  evidence_media_id: string;
  expires_at: string;
  factor: string;
  ledger_write: boolean;
  product_id: string;
  receipt_ref: string;
  scope: string;
  session_id: string;
  session_version: number;
  source_unit: string;
  status: string;
  target_unit: string;
  verified_at: string;
  verified_by: string;
}

export interface ConversionVerify {
  effective_from: string;
  evidence_media_id: string;
  expected_session_version: number;
  expires_at: string;
  factor: string;
  product_id: string;
  source_unit: "box";
  target_unit: "piece";
}

export interface DecisionEnvelope {
  code: string;
  data?: DecisionOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface DecisionOut {
  decided_by: string;
  decision: string;
  effect?: "review_only";
  id: string;
  input_sha256: string;
  reason: string;
  replayed: boolean;
  rule_id: string;
  rule_version: number;
  version: number;
}

export interface DocumentCreate {
  currency: "CNY" | "USD";
  document_key: string;
  document_type: "quotation" | "reconciliation" | "receiving_list";
  evidence_id: string;
  evidence_sha256: string;
  expected_version: number;
  lines: Line[];
  title: string;
}

export interface DocumentDecision {
  content_sha256: string;
  decision: "approved" | "rejected";
  expected_version: number;
  reason: string;
}

export interface DocumentEnvelope {
  code: string;
  data?: Record<string, unknown> | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface DocumentExport {
  content_sha256: string;
  expected_version: number;
  file_format: "xlsx" | "pdf";
}

export interface DraftCreate {
  currency: string;
  quote_id?: string | null;
}

export interface DraftEnvelope {
  code: string;
  data: DraftOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface DraftLineOut {
  confirmed_at: string | null;
  confirmed_by: string | null;
  discount_bps: number;
  id: string;
  line_amount: string;
  line_key: string;
  product_id: string;
  product_name: string;
  quantity: string;
  quote_id?: string | null;
  unit_price: string;
}

export interface DraftListEnvelope {
  code: string;
  data: DraftOut[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface DraftOut {
  created_at: string;
  creator_id: string;
  currency: string;
  expires_at: string | null;
  handoffs?: HandoffOut[];
  id: string;
  lines?: DraftLineOut[];
  organization_id: string;
  quote_id: string | null;
  stage: "draft" | "ready";
  status: "pending" | "paid";
  store_id: string;
  sync_state: "local_only" | "pending" | "synced" | "conflict";
  total_amount: string;
  updated_at: string;
  version: number;
  voided_at: string | null;
}

export interface ERPApprovalReceipt {
  approved_by: string;
  decision: "approved" | "rejected";
  expires_at: string;
  id: string;
  reason: string;
  request_digest: string;
}

export interface ERPCommandDecide {
  decision: "approved" | "rejected";
  expires_at: string;
  reason: string;
  request_digest: string;
}

export interface ERPCommandEnvelope {
  code: string;
  data?: ERPCommandOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ERPCommandOut {
  approval: ERPApprovalReceipt | null;
  expires_at: string;
  id: string;
  replayed?: boolean;
  request: ERPRequestDetails;
  request_digest: string;
  state: "pending" | "approved" | "rejected" | "staged" | "unknown" | "manual_rejected" | "verified_external";
  target_digest: string;
}

export interface ERPCommandPropose {
  action: "catalog_field_proposal" | "purchase_document_proposal" | "sales_return_proposal" | "sales_document_proposal" | "goods_receipt_document_proposal" | "catalog_maintenance_proposal";
  business_parameters: Record<string, unknown>;
  expires_at: string;
  source_sha256: string;
  target_id: string;
  target_version: number;
}

export interface ERPCommandStage {
  approval_id: string;
  expected_target_version: number;
  request_digest: string;
}

export interface ERPRequestDetails {
  action: "catalog_field_proposal" | "purchase_document_proposal" | "sales_return_proposal" | "sales_document_proposal" | "goods_receipt_document_proposal" | "catalog_maintenance_proposal";
  business_parameters: Record<string, string>;
  requested_by: string;
  source_sha256: string;
  target_id: string;
  target_version: number;
}

export type EmptyCommandInput = Record<string, never>;

export interface EvidenceEnvelope {
  code: string;
  data: EvidenceOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface EvidenceListEnvelope {
  code: string;
  data: EvidencePage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface EvidenceOut {
  byte_size: number;
  content_type: string;
  created_at: string;
  created_by: string;
  id: string;
  original_name: string;
  sha256: string;
  source_channel: string;
  version: number;
}

export interface EvidencePage {
  items: EvidenceOut[];
}

export interface ExceptionSource {
  anomaly_present: boolean;
  can_resolve: boolean;
  effect?: "case_tracking_only";
  kind: "shortage" | "overage" | "wrong_item" | "identity_ambiguous" | "partial" | "unknown_write" | "overdue" | "price_change" | "workflow_attention";
  lines?: SourceLine[];
  mode?: "human_supervised";
  references?: SourceReference[];
  resolution_digest?: string | null;
  resolution_kind?: "verified_resolution" | "reconciled_partial" | "verified_rejection" | null;
  source_digest: string;
  source_id: string;
  source_state: string;
  source_type: "receiving_discrepancy" | "erp_sandbox" | "erp_intent" | "sales_handoff" | "workflow" | "task";
  source_verification: "employee_observation" | "synthetic_erp" | "manual_erp_evidence" | "sales_record" | "workflow_tracking" | "task_record";
  source_version: number | null;
}

export interface ExceptionSourceEnvelope {
  code: string;
  data: ExceptionSource | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ExtractRequest {
  evidence_id: string;
}

export interface ExtractionEnvelope {
  code: string;
  data: ExtractionOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ExtractionOut {
  content_sha256: string;
  created_at: string;
  evidence_id: string;
  evidence_sha256: string;
  evidence_version: number;
  extractor_key: string;
  extractor_version: string;
  fields: CandidateOut[];
  id: string;
  replayed: boolean;
  reviews?: Record<string, unknown>[];
  source_kind: string;
  spans: SpanOut[];
  warnings: string[];
}

export interface ExtractionProof {
  action?: "observe" | "perform";
  content_digest?: string | null;
  extraction_id?: string | null;
  kind: "extraction";
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

export interface HandoffCreate {
  external_ref?: string | null;
}

export interface HandoffEnvelope {
  code: string;
  data: HandoffOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface HandoffOut {
  created_at: string;
  external_ref: string | null;
  handoff_state: "requested" | "sent" | "receipt_unknown" | "confirmed" | "failed";
  id: string;
  updated_at: string;
}

export interface IdentityContext {
  auth_expires_at: string;
  capabilities?: string[];
  capability_version?: string;
  organization_id: string;
  store_id: string;
  subject_id: string;
}

export interface IdentityContextEnvelope {
  code: string;
  data: IdentityContext | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ImageBindInput {
  asset_id: string;
  operation: "image_bind";
  upload_command_id: string;
}

export interface ImageUploadInput {
  asset_id: string;
  operation: "image_upload";
}

export interface IntakeProof {
  kind: "intake";
  receipt_id: string;
}

export type JsonValue = unknown;

export interface KnowledgeCreate {
  body: string;
  expires_at?: string | null;
  scope_ref?: string | null;
  scope_type?: "store" | "product" | "category" | "global";
  source_ref?: string | null;
  source_type: "manual" | "file" | "link" | "import";
  title: string;
}

export interface KnowledgeDetailEnvelope {
  code: string;
  data: KnowledgeDetailOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface KnowledgeDetailOut {
  approved_at: string | null;
  approved_by: string | null;
  expires_at: string | null;
  history?: KnowledgeRevisionOut[];
  id: string;
  organization_id: string;
  revision?: KnowledgeRevisionOut | null;
  scope_ref: string | null;
  scope_type: string;
  status: string;
  store_id: string;
  submitted_at: string;
  submitted_by: string;
  version: number;
}

export interface KnowledgeEntryOut {
  approved_at: string | null;
  approved_by: string | null;
  expires_at: string | null;
  id: string;
  organization_id: string;
  revision?: KnowledgeRevisionOut | null;
  scope_ref: string | null;
  scope_type: string;
  status: string;
  store_id: string;
  submitted_at: string;
  submitted_by: string;
  version: number;
}

export interface KnowledgeEnvelope {
  code: string;
  data: KnowledgeEntryOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface KnowledgeListEnvelope {
  code: string;
  data: unknown | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface KnowledgeReview {
  decision: "approve" | "reject" | "verify_source";
  expected_version: number;
  reason?: string | null;
  revision_id: string;
}

export interface KnowledgeRevisionCreate {
  body: string;
  expected_version: number;
  expires_at?: string | null;
  scope_ref?: string | null;
  scope_type?: "store" | "product" | "category" | "global";
  source_ref?: string | null;
  source_type: "manual" | "file" | "link" | "import";
  title: string;
}

export interface KnowledgeRevisionOut {
  body: string;
  id: string;
  source_ref: string | null;
  source_type: string;
  submitted_at: string;
  submitted_by: string;
  title: string;
  version: number;
}

export interface KnowledgeWithdraw {
  expected_version: number;
  reason: string;
}

export interface Line {
  description: string;
  line_ref: string;
  quantity: string;
  unit: string;
  unit_price: string;
}

export interface LineCreate {
  discount_bps: number;
  line_key: string;
  product_id: string;
  product_name: string;
  quantity: number | string;
  quote_id?: string | null;
  unit_price: number | string;
}

export interface LineQuoteEnvelope {
  code: string;
  data: QuoteResponse | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface LineQuoteRequest {
  discount_bps: number;
  price_source: PriceSourceInput;
  product_id: string;
  quantity: number | string;
  quote_key: string;
  rule_key: string;
}

export interface LocationCreate {
  kind: string;
  name: string;
  parent_id?: string | null;
}

export interface LocationEnvelope {
  code: string;
  data: LocationOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface LocationOut {
  active: boolean;
  created_at: string;
  id: string;
  kind: string;
  name: string;
  parent_id: string | null;
  updated_at: string;
}

export interface LoginData {
  access_token: string;
  expires_at: string;
  organization_id: string;
  store_id: string;
  subject_id: string;
  token_type: "Bearer";
}

export interface LoginEnvelope {
  code: string;
  data: LoginData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface LoginRequest {
  password: string;
  username: string;
}

export interface MarkRead {
  expected_version: number;
}

export interface MediaAssetEnvelope {
  code: string;
  data: MediaAssetOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface MediaAssetListEnvelope {
  code: string;
  data: MediaAssetListPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface MediaAssetListPage {
  items: MediaAssetOut[];
}

export interface MediaAssetOut {
  byte_size: number;
  category: string;
  content_type: string;
  created_at: string;
  created_by: string;
  id: string;
  original_name: string;
  sha256: string;
  source: string;
}

export interface MemoryCorrect {
  content: string;
  expected_version: number;
  expires_at?: string | null;
  purpose?: string | null;
}

export interface MemoryCreate {
  content: string;
  expires_at?: (string | null);
  persist?: boolean;
  purpose?: (string | null);
  retain_until_withdrawn?: boolean;
}

export interface MemoryEnvelope {
  code: string;
  data: MemoryOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface MemoryListEnvelope {
  code: string;
  data: MemoryPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface MemoryOut {
  active: boolean;
  content: string;
  created_at: string;
  expires_at: (string | null);
  id: string;
  purpose: string;
  retain_until_withdrawn?: boolean;
  source: string;
  updated_at: string;
  version: number;
}

export interface MemoryPage {
  items: MemoryOut[];
}

export interface NotificationEnvelope {
  code: string;
  data: NotificationOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface NotificationListEnvelope {
  code: string;
  data: NotificationOut[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface NotificationOut {
  created_at: string;
  id: string;
  kind: string;
  read_at: string | null;
  rule_version: string;
  task_id: string;
  version: number;
}

export interface ObservationCreate {
  expected_session_version: number;
  kind?: "observed" | "correction";
  location_id?: string | null;
  media_id?: string | null;
  observed_at: string;
  product_id?: string | null;
  quantity: number | string;
  supersedes_id?: string | null;
  unit: "piece" | "box" | "kg" | "meter";
  unknown_location_label?: string | null;
  unknown_product_label?: string | null;
}

export interface ObservationEnvelope {
  code: string;
  data: ObservationOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ObservationOut {
  event_seq: number;
  id: string;
  kind: string;
  location_id: string | null;
  media_id: string | null;
  observed_at: string;
  observed_by: string;
  product_id: string | null;
  quantity: string;
  recorded_at: string;
  session_id: string;
  supersedes_id: string | null;
  unit: string;
  unknown_location_label: string | null;
  unknown_product_label: string | null;
}

export interface ObservationsEnvelope {
  code: string;
  data: ObservationOut[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface PasswordChangeRequest {
  current_password: string;
  new_password: string;
}

export interface PasteIn {
  text: string;
}

export interface PendingSuggestionRecord {
  actor_subject_id: string;
  created_at: string;
  current_value?: unknown;
  field: string;
  op_id: string;
  reason?: string | null;
  suggested_value: unknown;
}

export interface PriceSourceInput {
  currency: string;
  expires_at?: string | null;
  observed_at: string;
  source_ref: string;
  source_type: string;
  source_version: string;
}

export interface PurchaseEvidenceOut {
  consent_confirmed: boolean;
  content_sha256: string;
  created_at: string;
  created_by: string;
  id: string;
  original_text: string;
  source_kind: "wechat_paste" | "delivery_note_text";
  supplier_name: string;
}

export interface PurchaseIntentCreate {
  consent_confirmed: boolean;
  line_descriptions: string[];
  original_text: string;
  source_kind: "wechat_paste" | "delivery_note_text";
  supplier_name: string;
}

export interface PurchaseIntentEnvelope {
  code: string;
  data: PurchaseIntentOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface PurchaseIntentListEnvelope {
  code: string;
  data: PurchaseIntentOut[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface PurchaseIntentOut {
  created_at: string;
  creator_id: string;
  evidence: PurchaseEvidenceOut;
  id: string;
  lines: PurchaseLineOut[];
  organization_id: string;
  status: "awaiting_arrival";
  store_id: string;
  updated_at: string;
  version: number;
}

export interface PurchaseLineConfirm {
  expected_version: number;
  product_id: string;
  product_version: number;
  quantity: number | string;
}

export interface PurchaseLineOut {
  confirmed_at: string | null;
  confirmed_by: string | null;
  id: string;
  original_description: string;
  product_id: string | null;
  product_version: number | null;
  quantity: string | null;
}

export interface QuoteResponse {
  amount_precision: number;
  cost_amount?: string | null;
  cost_observed_at?: string | null;
  cost_source_ref?: string | null;
  cost_source_type?: string | null;
  created_at: string;
  currency: string;
  discount_bps: number;
  expires_at: string;
  final_amount: string;
  id: string;
  list_price: string;
  object_id: string;
  object_type: string;
  object_version: string;
  price_source_ref: string;
  price_source_type: string;
  price_source_version: string;
  product_id: string;
  quantity: string;
  quote_key: string;
  requires_approval: boolean;
  rounding: string;
  rule_key: string;
  rule_version: number;
  snapshot_digest: string;
  source_expires_at: string | null;
  source_observed_at: string;
  status: "quoted" | "approval_required";
  unit_price: string;
}

export interface ReceiptAttemptView {
  created_at: string;
  delivery_ref: string | null;
  dispatched_at: string | null;
  executor_id: string;
  external_ref: string | null;
  id: string;
  input_digest: string;
  intent_id: string;
  line_results: ReceiptLineResult[];
  receipt_digest: string | null;
  state: "prepared" | "dispatched" | "unknown" | "partial" | "sandbox_verified" | "failed";
  updated_at: string;
  version: number;
}

export interface ReceiptBatch {
  confirmed_at: string;
  confirmed_by: string;
  created_at: string;
  created_by: string;
  decisions: ReceiptDecision[];
  delivery_note_sha256: string;
  expected_intent_version: number;
  id: string;
  intent_id: string;
  lines: ReceiptLine[];
  status: "reviewed";
  version: number;
}

export interface ReceiptCommandEnvelope {
  code: string;
  data: ReceiptCommandView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReceiptCommandStatus {
  command: ReceiptCommandView | null;
  status: "found" | "not_found";
}

export interface ReceiptCommandStatusEnvelope {
  code: string;
  data: ReceiptCommandStatus | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReceiptCommandView {
  attempt: ReceiptAttemptView | null;
  batch_id: string;
  created_at: string;
  delivery_note_sha256: string;
  effect: "local_sandbox_only";
  id: string;
  inventory_state: "not_posted";
  mode: "synthetic";
  proposal_digest: string;
  purchase_intent_id: string;
  purchase_state: "not_verified";
  replayed: boolean;
  request: ReceiptERPCommandOut;
  route: "api";
  source_digest: string;
  source_snapshot: ReceiptSourceSnapshot;
  source_version: number;
  state: "prepared" | "dispatched" | "unknown" | "partial" | "sandbox_verified" | "failed" | "pending" | "approved" | "rejected" | "staged" | "manual_rejected";
}

export interface ReceiptCreate {
  external_ref: string;
  paid?: boolean;
  unknown?: boolean;
}

export interface ReceiptCumulative {
  expected_quantity: string;
  final_batch_id: string | null;
  intent_line_id: string;
  observed_quantity: string;
  product_id: string;
  unit: "piece" | "box" | "kg" | "meter" | null;
}

export interface ReceiptDecideInput {
  decision: "approved" | "rejected";
  expires_at: string;
  reason: string;
  request_digest: string;
}

export interface ReceiptDecision {
  batch_version: number;
  created_at: string;
  decision: "accept_observation";
  detail_digest: string;
  discrepancy_id: string;
  evidence_sha256: string;
  id: string;
  kind: "shortage" | "overage" | "wrong_item" | "price_change" | "identity_unknown";
  line_id: string;
  responsible_id: string;
  reviewer_id: string;
}

export interface ReceiptERPApprovalReceipt {
  approved_by: string;
  decision: "approved" | "rejected";
  expires_at: string;
  id: string;
  reason: string;
  request_digest: string;
}

export interface ReceiptERPCommandOut {
  approval: ReceiptERPApprovalReceipt | null;
  expires_at: string;
  id: string;
  replayed?: boolean;
  request: ReceiptERPRequestDetails;
  request_digest: string;
  state: "pending" | "approved" | "rejected" | "staged" | "unknown" | "manual_rejected";
  target_digest: string;
}

export interface ReceiptERPRequestDetails {
  action: "goods_receipt_document_proposal";
  business_parameters: Record<string, string>;
  requested_by: string;
  source_sha256: string;
  target_id: string;
  target_version: number;
}

export interface ReceiptEnvelope {
  code: string;
  data: ReceiptOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReceiptIntent {
  evidence_id: string;
  evidence_sha256: string;
  id: string;
  lines: ReceiptIntentLine[];
  status: "awaiting_arrival";
  supplier_name_claim: string;
  version: number;
}

export interface ReceiptIntentLine {
  confirmed_at: string;
  confirmed_by: string;
  id: string;
  product_id: string;
  product_version: number;
  quantity: string;
}

export interface ReceiptLine {
  delivered_price: string | null;
  evidence_sha256: string;
  final_for_intent_line: boolean;
  id: string;
  intent_line_id: string;
  line_key: string;
  line_total: string | null;
  observed_at: string;
  observed_by: string;
  price_evidence_sha256: string | null;
  product_id: string;
  product_version: number;
  quantity: string;
  quoted_price: string | null;
  unit: "piece" | "box" | "kg" | "meter";
}

export interface ReceiptLineResult {
  error_code: string | null;
  external_line_ref: string | null;
  line_id: string;
  status: "pending" | "accepted" | "rejected" | "unknown";
}

export interface ReceiptObservationPage {
  attempt_id: string | null;
  command_id: string;
  items: ReceiptObservationView[];
  next_cursor: string | null;
}

export interface ReceiptObservationPageEnvelope {
  code: string;
  data: ReceiptObservationPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReceiptObservationView {
  actor_id: string;
  attempt_id: string;
  created_at: string;
  evidence_digest: string;
  id: string;
  kind: "dispatch_intent" | "ack" | "readback" | "network_error";
}

export interface ReceiptOut {
  byte_size: number;
  content_type: string;
  created_at: string;
  created_by: string;
  id: string;
  original_name: string;
  receipt_id: string;
  replayed: boolean;
  sha256: string;
  source_channel: string;
  version: number;
}

export interface ReceiptProposeInput {
  batch_id: string;
  expected_batch_version: number;
  expected_source_digest: string;
  expires_at: string;
  reason: string;
}

export interface ReceiptReferences {
  kind: "shortage" | "overage" | "wrong_item" | "identity_ambiguous" | "partial" | "unknown_write" | "overdue" | "price_change" | "workflow_attention";
  resolution_kind?: "verified_resolution" | "reconciled_partial" | "verified_rejection" | null;
  source_digest: string;
  source_id: string;
  source_state: string;
  source_type: "receiving_discrepancy" | "erp_sandbox" | "erp_intent" | "sales_handoff" | "workflow" | "task";
  source_verification: "employee_observation" | "synthetic_erp" | "manual_erp_evidence" | "sales_record" | "workflow_tracking" | "task_record";
  source_version: number | null;
}

export interface ReceiptSourceContext {
  snapshot: ReceiptSourceSnapshot;
  source_digest: string;
}

export interface ReceiptSourceEnvelope {
  code: string;
  data: ReceiptSourceContext | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReceiptSourceSnapshot {
  batch_id: string;
  batch_version: number;
  cumulative: ReceiptCumulative[];
  currency: "CNY";
  currency_source: "synthetic_contract_only";
  effect: "local_sandbox_only";
  intent: ReceiptIntent;
  organization_id: string;
  related_batches: ReceiptBatch[];
  store_id: string;
  total_amount: string;
}

export interface ReceiptStageInput {
  approval_id: string;
  expected_batch_version: number;
  request_digest: string;
}

export interface ReconcileEnvelope {
  code: string;
  data: ReconcileOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReconcileOut {
  created: number;
  rule_version: string;
  scanned: number;
}

export interface ResolutionEnvelope {
  code: string;
  data?: ResolutionOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ResolutionOut {
  converted_quantity?: string | null;
  decision_evidence: string | null;
  effect?: "candidate_only";
  explanation: string;
  input_sha256: string | null;
  kind: string;
  pending_human: boolean;
  rule_id: string | null;
  rule_version: number | null;
  scope_key: string;
  source_ref: string | null;
  source_sha256: string | null;
  status: "resolved" | "needs_human_conflict" | "unknown_or_unapproved";
}

export interface ResolvedBlockRecord {
  degraded?: boolean;
  fallback_text?: string | null;
  props: Record<string, unknown>;
  provenance: BlockProvenance;
  reason?: string | null;
  refs: Record<string, unknown>[];
  resolved_data?: Record<string, unknown>;
  status_kind: "development" | "production" | "suggestion" | "fact" | "pending_sync" | "settled" | "verified" | "unverified" | "waiting" | "error";
  type: string;
  version: string;
}

export interface ReviewDecision {
  decision: "accepted_partial" | "needs_recount";
  expected_session_version: number;
  observation_ids: string[];
  reason: string;
}

export interface ReviewItemCreate {
  diff_payload: Record<string, unknown>;
  risk_level: "low" | "medium" | "high" | "critical";
  summary: string;
  target_id: string;
  target_kind: "workflow" | "exception" | "document" | "workpaper" | "settlement" | "catalog";
  target_version: number;
  title: string;
}

export interface ReviewItemDecide {
  decision: "approved" | "rejected" | "modified" | "escalated";
  expected_target_version: number;
  item_id: string;
  lock_token?: string | null;
  modifications?: Record<string, unknown> | null;
  reason: string;
}

export interface ReviewOut {
  corrected_text: string | null;
  created_at: string;
  decision: string;
  effect?: "candidate_review_only";
  extraction_id: string;
  extraction_sha256: string;
  field_id: string;
  id: string;
  reason: string;
  replayed: boolean;
  reviewed_by: string;
  version: number;
}

export interface ReviewPreviewEnvelope {
  code: string;
  data: ReviewPreviewView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReviewPreviewView {
  book_difference: null;
  book_quantity: null;
  conversion_receipts: Record<string, unknown>[];
  coverage_kind: string;
  coverage_location_id: string | null;
  coverage_note: string;
  difference_status: string;
  disclaimer: string;
  full_store_coverage: boolean;
  groups: Record<string, unknown>[];
  inventory_accuracy: null;
  observation_count: number;
  observations: Record<string, unknown>[];
  preview_as_of: string;
  review_current: boolean;
  review_receipts: Record<string, unknown>[];
  reviewer_id: string | null;
  session_id: string;
  session_version: number;
}

export interface ReviewProof {
  extraction_id: string;
  kind: "review";
  review_digest: string;
}

export interface ReviewReceiptEnvelope {
  code: string;
  data: ReviewReceiptView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ReviewReceiptView {
  book_difference: null;
  coverage_kind: string;
  coverage_location_id: string | null;
  decision: string;
  full_store_coverage: boolean;
  ledger_write: boolean;
  observation_ids: string[];
  reason: string;
  receipt_ref: string;
  reviewed_at: string;
  reviewer_id: string;
  session_id: string;
  session_version: number;
  stock_adjustment: null;
}

export interface ReviewRequest {
  corrected_text?: string | null;
  decision: "confirmed" | "rejected" | "corrected";
  expected_version: 0;
  extraction_sha256: string;
  field_id: string;
  reason: string;
}

export interface RuleDecide {
  decision: "approved" | "rejected" | "revoked";
  expected_version: number;
  input_sha256: string;
  reason: string;
}

export interface RuleEnvelope {
  code: string;
  data?: RuleOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface RuleOut {
  body_sha256: string;
  decision_state: string;
  decision_version: number;
  effect?: "candidate_only";
  id: string;
  kind: string;
  replayed: boolean;
  rule_key: string;
  scope_key: string;
  source_ref: string;
  source_sha256: string;
  submitted_by: string;
  valid_from: string;
  valid_until: string | null;
  version: number;
}

export interface RuleSubmit {
  kind: "customer_mapping" | "supplier_mapping" | "package_conversion" | "return_policy" | "exchange_policy";
  rule_body: Record<string, unknown>;
  rule_key: string;
  scope_key: string;
  source_ref: string;
  source_sha256: string;
  valid_from: string;
  valid_until?: string | null;
  version: number;
}

export type RunState = "created" | "running" | "waiting_for_human" | "waiting_for_external" | "completed" | "failed" | "cancelled";

export interface SalesAttemptView {
  created_at: string;
  delivery_ref: string | null;
  dispatched_at: string | null;
  executor_id: string;
  external_ref: string | null;
  id: string;
  input_digest: string;
  intent_id: string;
  line_results: SalesLineResult[];
  receipt_digest: string | null;
  state: "prepared" | "dispatched" | "unknown" | "partial" | "sandbox_verified" | "failed";
  updated_at: string;
  version: number;
}

export interface SalesCommandEnvelope {
  code: string;
  data: SalesCommandView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SalesCommandStatus {
  command: SalesCommandView | null;
  status: "found" | "not_found";
}

export interface SalesCommandStatusEnvelope {
  code: string;
  data: SalesCommandStatus | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SalesCommandView {
  attempt: SalesAttemptView | null;
  created_at: string;
  draft_id: string;
  effect: "local_sandbox_only";
  handoff_id: string;
  id: string;
  mode: "synthetic";
  payment_state: "not_verified";
  proposal_digest: string;
  replayed: boolean;
  request: SalesERPCommandOut;
  route: "api" | "pos";
  source_digest: string;
  source_snapshot: SalesSourceSnapshot;
  source_version: number;
  state: "pending" | "approved" | "rejected" | "staged" | "manual_rejected" | "prepared" | "dispatched" | "unknown" | "partial" | "sandbox_verified" | "failed";
}

export interface SalesDecideInput {
  decision: "approved" | "rejected";
  expires_at: string;
  reason: string;
  request_digest: string;
}

export interface SalesERPApprovalReceipt {
  approved_by: string;
  decision: "approved" | "rejected";
  expires_at: string;
  id: string;
  reason: string;
  request_digest: string;
}

export interface SalesERPCommandOut {
  approval: SalesERPApprovalReceipt | null;
  expires_at: string;
  id: string;
  replayed?: boolean;
  request: SalesERPRequestDetails;
  request_digest: string;
  state: "pending" | "approved" | "rejected" | "staged" | "unknown" | "manual_rejected";
  target_digest: string;
}

export interface SalesERPRequestDetails {
  action: "sales_document_proposal";
  business_parameters: Record<string, string>;
  requested_by: string;
  source_sha256: string;
  target_id: string;
  target_version: number;
}

export interface SalesLineResult {
  error_code: string | null;
  external_line_ref: string | null;
  line_id: string;
  status: "pending" | "accepted" | "rejected" | "unknown";
}

export interface SalesObservationPage {
  attempt_id: string | null;
  command_id: string;
  items: SalesObservationView[];
  next_cursor: string | null;
}

export interface SalesObservationPageEnvelope {
  code: string;
  data: SalesObservationPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SalesObservationView {
  actor_id: string;
  attempt_id: string;
  created_at: string;
  evidence_digest: string;
  id: string;
  kind: "dispatch_intent" | "ack" | "readback" | "network_error";
}

export interface SalesProposeInput {
  draft_id: string;
  expected_draft_version: number;
  expected_source_digest: string;
  expires_at: string;
  reason: string;
  route: "api" | "pos";
}

export interface SalesSourceContext {
  snapshot: SalesSourceSnapshot;
  source_digest: string;
}

export interface SalesSourceEnvelope {
  code: string;
  data: SalesSourceContext | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SalesSourceLine {
  confirmed_at: string;
  confirmed_by: string;
  discount_bps: number;
  discounted_unit_price: string;
  id: string;
  line_amount: string;
  line_key: string;
  product_id: string;
  product_name: string;
  quantity: string;
  quote_id: string;
  unit_price: string;
}

export interface SalesSourceSnapshot {
  currency: string;
  draft_id: string;
  draft_version: number;
  expires_at: string | null;
  handoff_id: string | null;
  lines: SalesSourceLine[];
  organization_id: string;
  stage: "ready";
  status: "pending";
  store_id: string;
  total_amount: string;
}

export interface SalesStageInput {
  approval_id: string;
  expected_draft_version: number;
  request_digest: string;
}

export interface SandboxCommandRef {
  command_id: string;
  request_digest: string;
}

export interface SandboxExecutionEnvelope {
  code: string;
  data?: SandboxExecutionReceipt | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SandboxExecutionReceipt {
  adapter_id?: "synthetic.erp.v1";
  created_at: string;
  dispatched_at?: string | null;
  id: string;
  input_digest: string;
  lines: SandboxLineReceipt[];
  mode?: "synthetic";
  protocol_version?: 1;
  replayed?: boolean;
  state: "prepared" | "dispatched" | "unknown" | "partial" | "sandbox_verified" | "failed";
  updated_at: string;
  version: number;
}

export interface SandboxLineReceipt {
  command_id: string;
  error_code?: string | null;
  external_ref?: string | null;
  id: string;
  input_digest: string;
  intent_id: string;
  line_no: number;
  receipt_digest?: string | null;
  state: "pending" | "unknown" | "sandbox_verified" | "failed";
  version: number;
}

export interface SandboxQueueInput {
  adapter_id?: "synthetic.erp.v1";
  commands: SandboxCommandRef[];
}

export interface ScheduleApproveInput {
  expected_template_digest: string;
  expected_version: number;
  expires_at: string;
  machine_id: string;
  max_occurrences: number;
  reason: string;
  valid_from: string;
}

export interface ScheduleCommandStatus {
  schedule?: ScheduleView | null;
  status: "found" | "not_found";
}

export interface ScheduleCommandStatusEnvelope {
  code: string;
  data: ScheduleCommandStatus | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ScheduleCreateInput {
  description: string;
  kind: "task_template" | "review_summary";
  reason: string;
  reviewer_id: string;
  rule: CalendarRuleInput;
  start_date: string;
  title: string;
}

export interface ScheduleEnvelope {
  code: string;
  data: ScheduleView | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ScheduleGrantView {
  approved_at: string;
  approved_by: string;
  authorization_epoch: number;
  authorization_state: "valid" | "not_yet_valid" | "expired" | "exhausted" | "needs_reapproval";
  expires_at: string;
  id: string;
  machine_id: string;
  max_occurrences: number;
  remaining_occurrences: number;
  reserved_occurrences: number;
  reviewer_id: string;
  schedule_id: string;
  schedule_version: number;
  template_digest: string;
  template_snapshot: ScheduleTemplateInput;
  used_occurrences: number;
  valid_from: string;
}

export interface ScheduleListEnvelope {
  code: string;
  data: SchedulePage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ScheduleOccurrencePage {
  items: ScheduleOccurrenceView[];
  next_cursor?: string | null;
  schedule_id: string;
}

export interface ScheduleOccurrencePageEnvelope {
  code: string;
  data: ScheduleOccurrencePage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ScheduleOccurrenceView {
  created_at: string;
  decision_code: string;
  grant_id: string;
  id: string;
  local_date: string;
  schedule_id: string;
  schedule_version: number;
  scheduled_at?: string | null;
  source_digest?: string | null;
  state: "queued" | "materialized" | "skipped" | "cancelled" | "needs_reapproval";
  task_id?: string | null;
  updated_at: string;
  version: number;
  window_end?: string | null;
}

export interface SchedulePage {
  items: ScheduleSummary[];
  next_cursor?: string | null;
}

export interface SchedulePauseInput {
  expected_template_digest: string;
  expected_version: number;
  reason: string;
}

export interface ScheduleReceiptPage {
  items: ScheduleReceiptView[];
  next_cursor?: string | null;
  schedule_id: string;
}

export interface ScheduleReceiptPageEnvelope {
  code: string;
  data: ScheduleReceiptPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ScheduleReceiptView {
  actor_id: string;
  created_at: string;
  grant_id?: string | null;
  id: string;
  occurrence_id?: string | null;
  operation: "create" | "replace" | "approve" | "pause" | "materialize" | "skip" | "cancel" | "invalidate" | "checkpoint";
  references: ScheduleReferences;
  request_digest: string;
  schedule_id: string;
  schedule_version: number;
  task_id?: string | null;
}

export interface ScheduleReferences {
  current_epoch?: number | null;
  decision_code?: string | null;
  fact_ids?: string[] | null;
  local_date?: string | null;
  outbox_event_id?: string | null;
  reason_digest?: string | null;
  result_schedule_version?: number | null;
  skipped_days?: number | null;
  skipped_from?: string | null;
  skipped_to?: string | null;
  source_digest?: string | null;
  template_digest?: string | null;
  total_count?: number | null;
  window_end?: string | null;
  window_start?: string | null;
}

export interface ScheduleReplaceInput {
  description: string;
  expected_template_digest: string;
  expected_version: number;
  kind: "task_template" | "review_summary";
  reason: string;
  reviewer_id: string;
  rule: CalendarRuleInput;
  start_date: string;
  title: string;
}

export interface ScheduleSummary {
  created_at: string;
  created_by: string;
  id: string;
  kind: "task_template" | "review_summary";
  last_checked_at?: string | null;
  reviewer_id: string;
  start_date: string;
  state: "draft" | "active" | "paused" | "needs_reapproval";
  template_digest: string;
  title: string;
  updated_at: string;
  version: number;
}

export interface ScheduleTemplateInput {
  description: string;
  kind: "task_template" | "review_summary";
  reviewer_id: string;
  rule: CalendarRuleInput;
  start_date: string;
  title: string;
}

export interface ScheduleView {
  authorization?: ScheduleGrantView | null;
  blocking_code?: string | null;
  command_receipt?: ScheduleReceiptView | null;
  created_at: string;
  created_by: string;
  description: string;
  effect?: "local_suggestion_only";
  id: string;
  kind: "task_template" | "review_summary";
  last_checked_at?: string | null;
  mode?: "human_supervised";
  replayed: boolean;
  reviewer_id: string;
  rule: CalendarRuleInput;
  start_date: string;
  state: "draft" | "active" | "paused" | "needs_reapproval";
  template_digest: string;
  title: string;
  updated_at: string;
  version: number;
}

export interface SearchEnvelope {
  code: string;
  data: SearchResultOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SearchMatchOut {
  barcode: string | null;
  captured_at: string;
  external_code: string;
  match_field: "code" | "barcode" | "alias" | "name" | "derived";
  match_type: "exact_code" | "exact_barcode" | "normalized_barcode" | "alias_exact" | "fuzzy_candidate";
  name: string | null;
  normalized_value: string | null;
  original_value: string;
  product_id: string;
  search_evidence?: Record<string, unknown>[];
  source_system: string;
  source_updated_at: string | null;
  source_version: string;
  spec: string | null;
  unit: string | null;
}

export interface SearchResultOut {
  ambiguous: boolean;
  candidate_total?: number;
  candidates: SearchMatchOut[];
  exact: SearchMatchOut[];
  exact_total?: number;
  has_more?: boolean;
  message: string;
  query: string;
  scan_truncated?: boolean;
  truncated?: boolean;
}

export interface SessionCreate {
  coverage_kind: "partial_area" | "partial_shelf" | "unknown";
  coverage_location_id?: string | null;
  coverage_note: string;
}

export interface SessionEnvelope {
  code: string;
  data: StocktakeSessionOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SessionsEnvelope {
  code: string;
  data: StocktakeSessionOut[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SettlementApprovalCreate {
  command_id: string;
  decision: "approved" | "rejected";
  expected_workpaper_version: number;
  reason?: string | null;
}

export interface SettlementEnvelope {
  code: string;
  data?: Record<string, unknown> | unknown[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface SettlementProposalCreate {
  currency: "CNY" | "USD";
  expected_workpaper_version: number;
  lines: AllocationLineProposal[];
  operation: "receipt_allocation" | "payment_allocation";
  party_ref: string;
  party_type: "customer" | "supplier";
  proposal_key: string;
  settlement_amount: string;
  workpaper_id: string;
}

export interface SettlementStageInput {
  command_id: string;
  queue_key: string;
}

export interface SourceEntry {
  amount?: string | null;
  date: string;
  direction: "debit" | "credit";
  evidence_locator: string;
  source_id: string;
  source_version: string;
}

export interface SourceLine {
  cumulative_observed_quantity?: string | null;
  error_code?: string | null;
  evidence_digest?: string | null;
  expected_quantity?: string | null;
  id: string;
  observed_quantity?: string | null;
  receipt_digest?: string | null;
  source_verification: "employee_observation" | "synthetic_erp" | "manual_erp_evidence" | "sales_record" | "workflow_tracking" | "task_record";
  state: string;
  unit?: string | null;
  version?: number | null;
}

export interface SourceReference {
  digest?: string | null;
  id: string;
  kind: string;
  version?: number | null;
}

export interface SpanOut {
  label: string | null;
  location: Record<string, unknown>;
  raw_text: string;
  span_id: string;
  warning: string | null;
}

export interface StocktakeSessionOut {
  coverage_kind: string;
  coverage_location_id: string | null;
  coverage_note: string;
  created_at: string;
  id: string;
  opened_by: string;
  status: string;
  version: number;
}

export interface TaskAssignment {
  assignee_id: string;
  due_at: string;
  reason: string;
}

export interface TaskCreate {
  description: string;
  proposal_ref?: string | null;
  reason: string;
  title: string;
}

export interface TaskDecision {
  choice: "accept" | "reject" | "defer" | "request_info";
  reason: string;
  supplement_ref?: string | null;
}

export interface TaskEnvelope {
  code: string;
  data: TaskResponse | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface TaskEventResponse {
  actor_id: string;
  decision_state: string | null;
  due_at: string | null;
  from_state: string | null;
  id: string;
  occurred_at: string;
  organization_id: string;
  reason: string;
  responsible_id: string | null;
  store_id: string;
  task_id: string;
  to_state: string;
}

export interface TaskEventsEnvelope {
  code: string;
  data: TaskEventResponse[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface TaskListEnvelope {
  code: string;
  data: TaskResponse[] | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface TaskManagedCreate {
  assignee_id?: string | null;
  description: string;
  due_at?: string | null;
  reason: string;
  title: string;
}

export interface TaskProof {
  action?: "observe" | "perform";
  kind: "execution" | "verification" | "closure";
  task_id: string;
  task_state_digest: string;
}

export interface TaskReason {
  reason: string;
}

export interface TaskResponse {
  accepted_at: string | null;
  accepted_by: string | null;
  assigned_at: string | null;
  assigned_by: string | null;
  assignee_id: string | null;
  close_reason: string | null;
  closed_at: string | null;
  closed_by: string | null;
  created_at: string;
  created_by: string;
  decision_state: "pending" | "accepted" | "rejected" | "deferred" | "info_requested";
  description: string;
  due_at: string | null;
  id: string;
  organization_id: string;
  proposal_ref: string | null;
  reviewer_id: string | null;
  source: "agent" | "human" | "schedule";
  started_at: string | null;
  started_by: string | null;
  state: "suggested" | "accepted" | "assigned" | "in_progress" | "verified" | "closed";
  store_id: string;
  title: string;
  verification_reason: string | null;
  verified_at: string | null;
  verified_by: string | null;
}

export interface TaskSuggestion {
  description: string;
  proposal_ref?: string | null;
  reason: string;
  reviewer_id: string;
  title: string;
}

export interface TextAction {
  current_value: string | null;
  field_name: "display_label" | "specification_note";
  new_value: string;
  operation: "text_update";
  original_value: string | null;
}

export interface TextFields {
  display_label: string | null;
  specification_note: string | null;
}

export interface TextUpdateInput {
  field_name: "display_label" | "specification_note";
  new_value: string;
  operation: "text_update";
}

export interface ThreadEventRecord {
  artifact_id?: string | null;
  created_at: string;
  event_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  seq: number;
  subject_id: string;
  thread_id: string;
}

export interface ThreadEventsData {
  cursor: number;
  events: ThreadEventRecord[];
  next_cursor: number;
  thread_id: string;
}

export interface ThreadEventsEnvelope {
  code: string;
  data?: ThreadEventsData | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ThreadWorkspaceEnvelope {
  code: string;
  data?: ThreadWorkspaceRecord | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface ThreadWorkspaceRecord {
  active_object_ref?: Record<string, unknown> | null;
  last_edited_at?: string | null;
  last_editor_device?: string | null;
  last_editor_subject_id?: string | null;
  open_objects?: Record<string, unknown>[];
  organization_id: string;
  recent_editor_hint?: string | null;
  store_id: string;
  subject_id: string;
  thread_id: string;
  updated_at?: string | null;
  version?: number;
}

export interface ThreadWorkspaceUpsertRequest {
  active_object_ref?: Record<string, unknown> | null;
  base_version?: number | null;
  device?: string;
  open_objects?: Record<string, unknown>[];
}

export interface UploadAction {
  asset: AssetSnapshot;
  operation: "image_upload";
}

export interface UploadProof {
  asset_id: string;
  asset_ref: string;
  asset_sha256: string;
  attempt_id: string;
  byte_size: number;
  command_id: string;
  content_type: "image/jpeg" | "image/png" | "image/webp";
  input_digest: string;
  product_id: string;
  receipt_digest: string;
  verification_state: "sandbox_verified";
}

export interface ValidationError {
  ctx?: Record<string, unknown>;
  input?: unknown;
  loc: Array<string | number>;
  msg: string;
  type: string;
}

export interface WorkflowAdvanceInput {
  expected_input_digest: string;
  expected_version: number;
  proof: IntakeProof | ExtractionProof | ReviewProof | ApprovalProof | TaskProof;
}

export interface WorkflowApprovalEnvelope {
  code: string;
  data: WorkflowApprovalReceipt | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface WorkflowApprovalInput {
  expected_input_digest: string;
  expected_version: number;
  expires_at: string;
  reason: string;
}

export interface WorkflowApprovalReceipt {
  action_key?: "office.workflow.execute.v1";
  approval_id: string;
  effect?: "workflow_tracking_only";
  execution_mode?: "manual_task";
  expires_at: string;
  input_digest: string;
  replayed?: boolean;
  state: "requested" | "approved" | "rejected" | "cancelled" | "expired";
  subject_id: string;
  subject_type?: "office_workflow";
  template_key?: "material_review.v1";
  template_version?: 1;
  workflow_id: string;
}

export interface WorkflowCommandReceipt {
  actor_id: string;
  created_at: string;
  id: string;
  input_digest: string;
  operation: "create" | "advance" | "pause" | "resume" | "fail" | "mark_unknown";
  output_digest: string;
  references: Record<string, JsonValue>;
  request_digest: string;
  step_no?: number | null;
  workflow_id: string;
  workflow_version: number;
}

export interface WorkflowContext {
  effect?: "workflow_tracking_only";
  evidence_digest: string;
  evidence_id: string;
  execution_mode?: "manual_task";
  origin_run_id?: string | null;
  rule_digest?: string | null;
  rule_id?: string | null;
  task_assignee_id?: string | null;
  task_context_digest: string;
  task_decision_state: "pending" | "accepted" | "rejected" | "deferred" | "info_requested";
  task_id: string;
  task_state: "suggested" | "accepted" | "assigned" | "in_progress" | "verified" | "closed";
  task_state_digest: string;
  template_key?: "material_review.v1";
  template_version?: 1;
}

export interface WorkflowContextEnvelope {
  code: string;
  data: WorkflowContext | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface WorkflowControlInput {
  expected_input_digest: string;
  expected_version: number;
  operation: "pause" | "resume" | "fail" | "mark_unknown";
  reason: string;
}

export interface WorkflowCreateInput {
  evidence_digest: string;
  evidence_id: string;
  execution_mode?: "manual_task";
  origin_run_id?: string | null;
  responsibilities: WorkflowResponsibility[];
  rule_digest?: string | null;
  rule_id?: string | null;
  task_context_digest: string;
  task_id: string;
  template_key?: "material_review.v1";
  template_version?: 1;
}

export interface WorkflowEnvelope {
  code: string;
  data: WorkflowReceipt | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface WorkflowReceipt {
  approval_input_digest?: string | null;
  blocking_code?: string | null;
  closed_at?: string | null;
  command_receipt?: WorkflowCommandReceipt | null;
  context?: WorkflowContext | null;
  created_at: string;
  effect?: "workflow_tracking_only";
  execution_mode?: "manual_task";
  id: string;
  input_digest: string;
  next_input_digest?: string | null;
  next_step_no: number;
  origin_run_id?: string | null;
  replayed?: boolean;
  review_digest?: string | null;
  rule_digest?: string | null;
  rule_id?: string | null;
  source_evidence_digest: string;
  source_evidence_id: string;
  state: "active" | "paused" | "needs_human" | "failed" | "closed";
  steps: WorkflowStepReceipt[];
  task_context_digest: string;
  task_id: string;
  template_key?: "material_review.v1";
  template_version?: 1;
  updated_at: string;
  version: number;
}

export interface WorkflowReceiptPage {
  effect?: "workflow_tracking_only";
  execution_mode?: "manual_task";
  next_after_version?: number | null;
  receipts: WorkflowCommandReceipt[];
  template_key?: "material_review.v1";
  template_version?: 1;
  workflow_id: string;
  workflow_version: number;
}

export interface WorkflowReceiptPageEnvelope {
  code: string;
  data: WorkflowReceiptPage | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface WorkflowResponsibility {
  kind: "intake" | "extraction" | "review" | "approval" | "execution" | "verification" | "closure";
  subject_id: string;
}

export interface WorkflowStepReceipt {
  completed_at?: string | null;
  input_digest?: string | null;
  kind: "intake" | "extraction" | "review" | "approval" | "execution" | "verification" | "closure";
  output_digest?: string | null;
  responsible_id: string;
  state: "pending" | "completed" | "failed" | "unknown";
  step_no: number;
  version: number;
}

export interface WorkpaperCreate {
  balance_convention?: "debit_increases" | "credit_increases" | null;
  closing_balance?: string | null;
  complete: boolean;
  currency: "CNY" | "USD";
  entries: SourceEntry[];
  evidence_id: string;
  evidence_sha256: string;
  expected_version: number;
  observed_at: string;
  opening_balance?: string | null;
  party_ref: string;
  party_type: "customer" | "supplier";
  period_end: string;
  period_start: string;
  source_kind: "synthetic";
  source_snapshot_id: string;
  source_version: string;
  workpaper_key: string;
}

export interface WorkpaperEnvelope {
  code: string;
  data?: Record<string, unknown> | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface backend__app__modules__office_extraction__ofc_003_schemas__ReviewEnvelope {
  code: string;
  data: ReviewOut | null;
  error: string | null;
  hint: string | null;
  success: boolean;
  user_message: string;
}

export interface backend__app__modules__office_review__ofc_009_schemas__ReviewEnvelope {
  code?: string;
  data?: unknown | null;
  error?: string | null;
  hint?: string;
  success: boolean;
  user_message?: string;
}

export interface ContextEnvelopeV2 {
  code: string;
  data: (QuoteContextV2 | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface EligibilityV2 {
  code: string;
  usable: boolean;
}

export interface FollowupRequest {
  previous_turn_id: string;
  request_id: string;
  text: string;
}

export interface InboxRequest {
  parent_id: string;
  request_id: string;
  text: string;
}

export interface LineBindRequestV2 {
  confirmed: true;
  line_key: string;
  quote_digest: string;
  quote_key: string;
}

export interface LineEnvelopeV2 {
  code: string;
  data: (LineLookupV2 | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface LineLookupV2 {
  current_eligibility: EligibilityV2;
  original_result: (LineResultV2 | null);
  outcome: "recorded" | "unknown";
}

export interface LineQuoteRequestV2 {
  catalog_product_id: string;
  context_digest: string;
  discount_bps: number;
  line_key: string;
  quantity: string;
  quote_key: string;
}

export interface LineResultV2 {
  catalog_product_id: string;
  currency: string;
  draft_id: string;
  id: string;
  line_amount: string;
  line_key: string;
  product_name: string;
  quantity: string;
  quote_id: string;
  quote_key: string;
  stage?: "draft";
  unit: string;
  unit_price: string;
  version?: "2";
}

export interface MemoryCommandStatus {
  current_version: (number | null);
  key: string;
  memory_id: (string | null);
  operation: "save" | "correct" | "withdraw";
  status: "unobserved" | "committed";
}

export interface MemoryCommandStatusEnvelope {
  code: string;
  data: (MemoryCommandStatus | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface MemoryNote {
  authority: "reference_only";
  filename: string;
  markdown: string;
  memory_id: string;
  version: number;
}

export interface MemoryNoteEnvelope {
  code: string;
  data: (MemoryNote | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface MemoryNoteImport {
  markdown: string;
}

export interface MemorySearchEnvelope {
  code: string;
  data: (MemorySearchPage | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface MemorySearchPage {
  authority: "reference_only";
  items: (MemoryOut)[];
  match_mode: "all_terms" | "any_terms" | "none";
}

export interface PiAbsentResult {
  reason: "result_absent";
  run_id: string;
  status: "not_recorded";
  tool_step_id: string;
}

export interface PiAvailableToolResult {
  canonical_ref: string;
  captured_at: string;
  kind: "success" | "tool_error";
  output_ref: string;
  payload: (PiMemoryResultPayload | PiSafeToolError);
  run_id: string;
  status: "available";
  tool_key: "memory.search";
  tool_step_id: string;
  tool_version: "1";
}

export interface PiMemoryResultItem {
  active: boolean;
  content: string;
  created_at: string;
  expires_at: (string | null);
  id: string;
  purpose: string;
  retain_until_withdrawn?: boolean;
  source: string;
  updated_at: string;
  version: number;
}

export interface PiMemoryResultPayload {
  authority: "reference_only";
  items: (PiMemoryResultItem)[];
  match_mode: "all_terms" | "any_terms" | "none";
}

export interface PiNativeToolReceipt {
  error_code: (string | null);
  model_step_id: string;
  native_call_id: string;
  output_ref: (string | null);
  pi_name: "v4_tasks_list_mine_v1" | "v4_knowledge_search_v1" | "v4_catalog_search_v1" | "v4_memory_search_v1" | "v4_memory_save_v1" | "v4_memory_correct_v1" | "v4_memory_withdraw_v1";
  receipt_id: string;
  state: "accepted" | "dispatch_intent" | "succeeded" | "failed" | "unknown";
  tool_key: "tasks.list_mine" | "knowledge.search" | "catalog.search" | "memory.search" | "memory.save" | "memory.correct" | "memory.withdraw";
  tool_step_id: (string | null);
  tool_version: "1";
  toolset_version: string;
}

export interface PiOnlineRunRuntimeReceipt {
  contract_version: number;
  launch: PiOnlineRuntimeLaunch;
  native_calls: (PiNativeToolReceipt)[];
  provider_calls: (PiProviderCallReceipt)[];
  recovery?: "A_stop_only";
  result_replay?: "unsupported";
  run_id: string;
  runtime: PiRuntimeIdentity;
  state: "created" | "running" | "waiting_for_human" | "waiting_for_external" | "completed" | "failed" | "cancelled";
  step_citation_association?: "unsupported";
  steps: (PiRuntimeStep)[];
}

export interface PiOnlineRuntimeLaunch {
  launch_profile: ("host-pi-readonly-node24-linux-online-v1" | "host-pi-readonly-node24-darwin-online-v1" | "host-pi-memory-node24-linux-online-v1" | "host-pi-memory-node24-darwin-online-v1" | null);
  reason: ("launch_record_absent" | "launch_record_unsupported" | null);
  source_manifest_sha256: (string | null);
  status: "recorded" | "not_recorded" | "unsupported";
  tool_profile: ("pi-readonly-v1" | "pi-memory-v1" | null);
  toolset_version: (string | null);
  wire_protocol: ("v4.pi-readonly/2" | "v4.pi-memory/2" | null);
}

export interface PiProviderCallReceipt {
  attempt_id: string;
  created_at: string;
  ledger_id: string;
  message_digest: (string | null);
  model_step_id: string;
  provider_request_id_sha256: (string | null);
  receipt_id: string;
  response_digest: (string | null);
  state: "permitted" | "send_intent" | "responded" | "unknown" | "not_sent";
  updated_at: string;
  usage_presence: (PiUsagePresence | null);
}

export interface PiRequest {
  run_id: string;
  text: string;
}

export interface PiRunRuntimeReceipt {
  contract_version: number;
  launch: PiRuntimeLaunch;
  native_calls: (PiNativeToolReceipt)[];
  recovery?: "A_stop_only";
  result_replay?: "unsupported";
  run_id: string;
  runtime: PiRuntimeIdentity;
  state: "created" | "running" | "waiting_for_human" | "waiting_for_external" | "completed" | "failed" | "cancelled";
  step_citation_association?: "unsupported";
  steps: (PiRuntimeStep)[];
}

export interface PiRuntimeIdentity {
  adapter_contract: ("host-run-receipt-v1" | null);
  basis: ("run_runtime_binding" | null);
  engine: ("@earendil-works/pi-coding-agent" | null);
  execution_proof?: "not_proven";
  reason: ("binding_absent" | "binding_unsupported" | null);
  status: "recorded" | "not_recorded" | "unsupported";
  version: ("1.4.0" | null);
}

export interface PiRuntimeLaunch {
  launch_profile: ("host-pi-readonly-node24-linux-v1" | "host-pi-readonly-node24-darwin-v1" | "host-pi-memory-node24-linux-v1" | "host-pi-memory-node24-darwin-v1" | null);
  reason: ("launch_record_absent" | "launch_record_unsupported" | null);
  source_manifest_sha256: (string | null);
  status: "recorded" | "not_recorded" | "unsupported";
  tool_profile: ("pi-readonly-v1" | "pi-memory-v1" | null);
  toolset_version: (string | null);
  wire_protocol: ("v4.pi-readonly/1" | "v4.pi-memory/1" | null);
}

export interface PiRuntimeReceiptEnvelope {
  code: string;
  data: ((PiRunRuntimeReceipt | PiOnlineRunRuntimeReceipt | PiStreamRunRuntimeReceipt) | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface PiRuntimeStep {
  error_code: (string | null);
  kind: "model" | "tool" | "checkpoint";
  output_ref: (string | null);
  sequence_no: number;
  state: "pending" | "running" | "succeeded" | "failed" | "unknown" | "cancelled";
  step_id: string;
}

export interface PiSafeToolError {
  code: "memory_query_invalid";
  field: "query";
  hint: "请使用1至256个有效字符和不超过16个查询词，移除空字符后重试。";
}

export interface PiStreamRunRuntimeReceipt {
  contract_version: number;
  launch: PiStreamRuntimeLaunch;
  native_calls: (PiNativeToolReceipt)[];
  provider_calls: (PiProviderCallReceipt)[];
  recovery?: "A_stop_only";
  result_replay?: "unsupported";
  run_id: string;
  runtime: PiRuntimeIdentity;
  state: "created" | "running" | "waiting_for_human" | "waiting_for_external" | "completed" | "failed" | "cancelled";
  step_citation_association?: "unsupported";
  steps: (PiRuntimeStep)[];
}

export interface PiStreamRuntimeLaunch {
  launch_profile: ("host-pi-readonly-node24-linux-online-stream-v1" | "host-pi-readonly-node24-darwin-online-stream-v1" | null);
  reason: ("launch_record_absent" | "launch_record_unsupported" | null);
  source_manifest_sha256: (string | null);
  status: "recorded" | "not_recorded" | "unsupported";
  tool_profile: ("pi-readonly-v1" | null);
  toolset_version: (string | null);
  wire_protocol: ("v4.pi-readonly/3" | null);
}

export interface PiToolResultReadEnvelope {
  code: string;
  data: ((PiAvailableToolResult | PiWithheldResult | PiAbsentResult) | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface PiTrialInfo {
  expires_at: string;
  mode: string;
  run_id: string;
  sample_sha256: string;
  sample_text: string;
  start_available: boolean;
}

export interface PiTrialInfoEnvelope {
  code: string;
  data: PiTrialInfo;
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface PiUsagePresence {
  cache_write_tokens: boolean;
  cached_tokens: boolean;
  completion_tokens: boolean;
  prompt_tokens: boolean;
}

export interface PiWithheldResult {
  reason: "binding_invalid" | "authorization_changed" | "source_changed" | "source_unsupported" | "source_incomplete";
  run_id: string;
  status: "withheld";
  tool_step_id: string;
}

export interface PublicTrace {
  reason?: ("record_binding_unavailable" | null);
  records: (PublicTraceRecord)[];
  state: "available" | "not_recorded" | "withheld";
  version: 1;
}

export interface PublicTraceProjection {
  arguments: Record<string, unknown>;
  authority: "projection_only";
  authorization_checked: false;
  binding_sha256: string;
  body_is_original_stream: false;
  captured_at: string;
  input_schema_sha256: string;
  input_sha256: string;
  model_step_id: string;
  native_call_sha256: string;
  output_schema_sha256: string;
  output_sha256: (string | null);
  phase: "accepted" | "result";
  projection_schema: (string | null);
  public_plan: Record<string, unknown>;
  receipt_id: string;
  result: Record<string, unknown>;
  run_id: string;
  source_authorization: "not_checked";
  time_semantics: "record_capture_not_execution_duration";
  tool_key: ("runtime.tool_search" | "memory.search" | "research.search" | "research.read" | "research.describe" | "research.propose" | null);
  tool_step_id: (string | null);
  tool_version: ("1" | null);
  version: 1;
}

export interface PublicTraceRecord {
  accepted?: (PublicTraceProjection | null);
  native_state: "accepted" | "dispatch_intent" | "succeeded" | "failed" | "unknown";
  reason?: ("current_source_unavailable" | null);
  receipt_id: string;
  result?: (PublicTraceProjection | null);
  state: "available" | "withheld";
  step_id: (string | null);
}

export interface QuoteContextV2 {
  captured_at: string;
  catalog_product_id: string;
  context_digest: string;
  currency: string;
  expires_at: string;
  external_code: string;
  list_price: string;
  observed_at: string;
  payload_sha256: string;
  policy_digest: string;
  policy_id: string;
  policy_version: number;
  product_name: string;
  product_version: number;
  rule_digest: string;
  rule_id: string;
  rule_version: number;
  snapshot_id: string;
  source_category?: "catalog";
  source_system: string;
  source_version: string;
  unit: string;
  version?: "2";
}

export interface QuoteEnvelopeV2 {
  code: string;
  data: (QuoteLookupV2 | null);
  error: (string | null);
  hint: (string | null);
  success: boolean;
  user_message: string;
}

export interface QuoteLookupV2 {
  current_eligibility: EligibilityV2;
  original_result: (QuoteResultV2 | null);
  outcome: "recorded" | "unknown";
}

export interface QuoteResultV2 {
  catalog_product_id: string;
  context: QuoteContextV2;
  created_at: string;
  discount_bps: number;
  draft_id: string;
  expires_at: string;
  final_amount: string;
  id: string;
  line_key: string;
  quantity: string;
  quote_key: string;
  snapshot_digest: string;
  status: "quoted" | "approval_required";
  unit_price: string;
  version?: "2";
}

export interface SdkRequest {
  run_id: string;
  text: string;
}

export interface WorkRequest {
  request_id: string;
  text: string;
}

export interface OpenApiPaths {
  "/api/v1/agent-costs": {
    get: { summary: "Cost Route" };
  };
  "/api/v1/agent-runs": {
    post: { summary: "Create Route" };
  };
  "/api/v1/agent-runs/{run_id}": {
    get: { summary: "Get Route" };
  };
  "/api/v1/agent-runs/{run_id}/cancel": {
    post: { summary: "Cancel Route" };
  };
  "/api/v1/agent-runs/{run_id}/confirmation": {
    post: { summary: "Confirmation Route" };
  };
  "/api/v1/analytics/snapshots": {
    get: { summary: "List Route" };
  };
  "/api/v1/analytics/snapshots/{snapshot_id}": {
    get: { summary: "Read Route" };
  };
  "/api/v1/approvals": {
    get: { summary: "List Route" };
    post: { summary: "Create Approval" };
  };
  "/api/v1/approvals/{approval_id}": {
    get: { summary: "Read Route" };
  };
  "/api/v1/approvals/{approval_id}/cancel": {
    post: { summary: "Cancel" };
  };
  "/api/v1/approvals/{approval_id}/decision": {
    post: { summary: "Decide" };
  };
  "/api/v1/approvals/{approval_id}/expire": {
    post: { summary: "Expire" };
  };
  "/api/v1/audit/events": {
    get: { summary: "Read Events" };
  };
  "/api/v1/auth/login": {
    post: { summary: "Login Route" };
  };
  "/api/v1/auth/logout": {
    post: { summary: "Logout Route" };
  };
  "/api/v1/auth/logout-all": {
    post: { summary: "Logout All Route" };
  };
  "/api/v1/auth/password": {
    post: { summary: "Password Route" };
  };
  "/api/v1/catalog": {
    get: { summary: "List Route" };
  };
  "/api/v1/catalog/cache": {
    get: { summary: "Cache Route" };
  };
  "/api/v1/catalog/conflicts": {
    get: { summary: "Conflicts Route" };
  };
  "/api/v1/catalog/{product_id}": {
    get: { summary: "Detail Route" };
  };
  "/api/v1/erp/catalog-maintenance": {
    post: { summary: "Propose Api" };
  };
  "/api/v1/erp/catalog-maintenance/commands/status": {
    get: { summary: "Status Api" };
  };
  "/api/v1/erp/catalog-maintenance/preview": {
    post: { summary: "Preview Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}": {
    get: { summary: "Read Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}/cancel": {
    post: { summary: "Cancel Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}/decide": {
    post: { summary: "Decide Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}/dispatch": {
    post: { summary: "Dispatch Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}/observations": {
    get: { summary: "Observations Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}/reconcile": {
    post: { summary: "Reconcile Api" };
  };
  "/api/v1/erp/catalog-maintenance/{command_id}/stage": {
    post: { summary: "Stage Api" };
  };
  "/api/v1/erp/commands": {
    post: { summary: "Command Propose" };
  };
  "/api/v1/erp/commands/{request_id}": {
    get: { summary: "Command Read" };
  };
  "/api/v1/erp/commands/{request_id}/decide": {
    post: { summary: "Command Decide" };
  };
  "/api/v1/erp/commands/{request_id}/stage": {
    post: { summary: "Command Stage" };
  };
  "/api/v1/erp/receiving": {
    post: { summary: "Propose Api" };
  };
  "/api/v1/erp/receiving/commands/status": {
    get: { summary: "Status Api" };
  };
  "/api/v1/erp/receiving/source-context/{batch_id}": {
    get: { summary: "Source Api" };
  };
  "/api/v1/erp/receiving/{command_id}": {
    get: { summary: "Read Api" };
  };
  "/api/v1/erp/receiving/{command_id}/decide": {
    post: { summary: "Decide Api" };
  };
  "/api/v1/erp/receiving/{command_id}/dispatch": {
    post: { summary: "Dispatch Api" };
  };
  "/api/v1/erp/receiving/{command_id}/observations": {
    get: { summary: "Observations Api" };
  };
  "/api/v1/erp/receiving/{command_id}/reconcile": {
    post: { summary: "Reconcile Api" };
  };
  "/api/v1/erp/receiving/{command_id}/stage": {
    post: { summary: "Stage Api" };
  };
  "/api/v1/erp/sales": {
    post: { summary: "Propose Api" };
  };
  "/api/v1/erp/sales/commands/status": {
    get: { summary: "Status Api" };
  };
  "/api/v1/erp/sales/source-context/{draft_id}": {
    get: { summary: "Source Api" };
  };
  "/api/v1/erp/sales/{command_id}": {
    get: { summary: "Read Api" };
  };
  "/api/v1/erp/sales/{command_id}/decide": {
    post: { summary: "Decide Api" };
  };
  "/api/v1/erp/sales/{command_id}/dispatch": {
    post: { summary: "Dispatch Api" };
  };
  "/api/v1/erp/sales/{command_id}/observations": {
    get: { summary: "Observations Api" };
  };
  "/api/v1/erp/sales/{command_id}/reconcile": {
    post: { summary: "Reconcile Api" };
  };
  "/api/v1/erp/sales/{command_id}/stage": {
    post: { summary: "Stage Api" };
  };
  "/api/v1/erp/sandbox-executions": {
    post: { summary: "Queue Api" };
  };
  "/api/v1/erp/sandbox-executions/{batch_id}": {
    get: { summary: "Read Api" };
  };
  "/api/v1/erp/sandbox-executions/{batch_id}/dispatch": {
    post: { summary: "Dispatch Api" };
  };
  "/api/v1/erp/sandbox-executions/{batch_id}/reconcile": {
    post: { summary: "Reconcile Api" };
  };
  "/api/v1/erp/settlement/attempts/{attempt_id}/dispatch": {
    post: { summary: "Dispatch Attempt" };
  };
  "/api/v1/erp/settlement/attempts/{attempt_id}/reconcile": {
    post: { summary: "Reconcile Attempt" };
  };
  "/api/v1/erp/settlement/decisions": {
    post: { summary: "Decide Proposal" };
  };
  "/api/v1/erp/settlement/proposals": {
    post: { summary: "Create Proposal" };
  };
  "/api/v1/erp/settlement/stages": {
    post: { summary: "Stage Command" };
  };
  "/api/v1/identity/context": {
    get: { summary: "Identity Context" };
  };
  "/api/v1/knowledge": {
    get: { summary: "List Route" };
    post: { summary: "Submit Route" };
  };
  "/api/v1/knowledge/review-queue": {
    get: { summary: "Review Queue Route" };
  };
  "/api/v1/knowledge/{entry_id}": {
    get: { summary: "Detail Route" };
  };
  "/api/v1/knowledge/{entry_id}/review": {
    post: { summary: "Review Route" };
  };
  "/api/v1/knowledge/{entry_id}/review-material": {
    get: { summary: "Review Material Route" };
  };
  "/api/v1/knowledge/{entry_id}/revisions": {
    post: { summary: "Revise Route" };
  };
  "/api/v1/knowledge/{entry_id}/withdraw": {
    post: { summary: "Withdraw Route" };
  };
  "/api/v1/locations": {
    post: { summary: "Create Route" };
  };
  "/api/v1/locations/{location_id}/assertions": {
    get: { summary: "History Route" };
    post: { summary: "Assert Route" };
  };
  "/api/v1/media/assets": {
    get: { summary: "List Route" };
    post: { summary: "Upload Route" };
  };
  "/api/v1/media/assets/{asset_id}": {
    get: { summary: "Detail Route" };
  };
  "/api/v1/media/assets/{asset_id}/content": {
    get: { summary: "Content Route" };
  };
  "/api/v1/memory": {
    get: { summary: "List Route" };
    post: { summary: "Save Route" };
  };
  "/api/v1/memory/export": {
    get: { summary: "Export Route" };
  };
  "/api/v1/memory/{memory_id}": {
    delete: { summary: "Withdraw Route" };
    get: { summary: "Detail Route" };
    patch: { summary: "Correct Route" };
  };
  "/api/v1/notifications": {
    get: { summary: "Inbox" };
  };
  "/api/v1/notifications/reconcile-overdue": {
    post: { summary: "Overdue" };
  };
  "/api/v1/notifications/{notification_id}/read": {
    post: { summary: "Read" };
  };
  "/api/v1/office-documents": {
    get: { summary: "Document List" };
    post: { summary: "Document Create" };
  };
  "/api/v1/office-documents/commands/status": {
    get: { summary: "Document Command Status" };
  };
  "/api/v1/office-documents/{identity}": {
    get: { summary: "Document Get" };
  };
  "/api/v1/office-documents/{identity}/export": {
    post: { summary: "Document Export" };
  };
  "/api/v1/office-documents/{identity}/exports/{file_format}": {
    get: { summary: "Document Read Export" };
  };
  "/api/v1/office-documents/{identity}/review": {
    post: { summary: "Document Review" };
  };
  "/api/v1/office-extraction/extractions": {
    post: { summary: "Extraction Post" };
  };
  "/api/v1/office-extraction/extractions/{extraction_id}": {
    get: { summary: "Extraction Get" };
  };
  "/api/v1/office-extraction/extractions/{extraction_id}/review": {
    post: { summary: "Extraction Review Post" };
  };
  "/api/v1/office-finance/workpapers": {
    get: { summary: "List Route" };
    post: { summary: "Create Route" };
  };
  "/api/v1/office-finance/workpapers/{identity}": {
    get: { summary: "Read Route" };
  };
  "/api/v1/office-intake/evidence": {
    get: { summary: "List Evidence" };
  };
  "/api/v1/office-intake/evidence/file": {
    post: { summary: "Upload File" };
  };
  "/api/v1/office-intake/evidence/text": {
    post: { summary: "Paste Text" };
  };
  "/api/v1/office-intake/evidence/{evidence_id}": {
    get: { summary: "Evidence Detail" };
  };
  "/api/v1/office-intake/evidence/{evidence_id}/content": {
    get: { summary: "Evidence Content" };
  };
  "/api/v1/office-rules/resolve": {
    get: { summary: "Rule Resolve" };
  };
  "/api/v1/office-rules/rules": {
    post: { summary: "Rule Post" };
  };
  "/api/v1/office-rules/rules/{rule_id}": {
    get: { summary: "Rule Get" };
  };
  "/api/v1/office-rules/rules/{rule_id}/decisions": {
    post: { summary: "Rule Review" };
  };
  "/api/v1/office/exceptions": {
    get: { summary: "List Api" };
    post: { summary: "Create Api" };
  };
  "/api/v1/office/exceptions/commands/status": {
    get: { summary: "Command Status Api" };
  };
  "/api/v1/office/exceptions/source-context": {
    get: { summary: "Source Api" };
  };
  "/api/v1/office/exceptions/{case_id}": {
    get: { summary: "Read Api" };
  };
  "/api/v1/office/exceptions/{case_id}/commands": {
    post: { summary: "Command Api" };
  };
  "/api/v1/office/exceptions/{case_id}/receipts": {
    get: { summary: "History Api" };
  };
  "/api/v1/office/review/batch-decide": {
    post: { summary: "Batch Decide" };
  };
  "/api/v1/office/review/export": {
    get: { summary: "Export Reviews" };
  };
  "/api/v1/office/review/items": {
    get: { summary: "List Items" };
    post: { summary: "Create Item" };
  };
  "/api/v1/office/review/items/{item_id}": {
    get: { summary: "Get Item" };
  };
  "/api/v1/office/review/items/{item_id}/claim": {
    post: { summary: "Claim Item" };
  };
  "/api/v1/office/review/items/{item_id}/decide": {
    post: { summary: "Decide Item" };
  };
  "/api/v1/office/review/items/{item_id}/release": {
    post: { summary: "Release Item" };
  };
  "/api/v1/office/schedules": {
    get: { summary: "List Api" };
    post: { summary: "Create Api" };
  };
  "/api/v1/office/schedules/commands/status": {
    get: { summary: "Status Api" };
  };
  "/api/v1/office/schedules/{schedule_id}": {
    get: { summary: "Read Api" };
    put: { summary: "Replace Api" };
  };
  "/api/v1/office/schedules/{schedule_id}/authorizations": {
    post: { summary: "Approve Api" };
  };
  "/api/v1/office/schedules/{schedule_id}/occurrences": {
    get: { summary: "Occurrences Api" };
  };
  "/api/v1/office/schedules/{schedule_id}/pause": {
    post: { summary: "Pause Api" };
  };
  "/api/v1/office/schedules/{schedule_id}/receipts": {
    get: { summary: "Receipts Api" };
  };
  "/api/v1/office/workflows": {
    post: { summary: "Create Api" };
  };
  "/api/v1/office/workflows/context": {
    get: { summary: "Context Api" };
  };
  "/api/v1/office/workflows/{workflow_id}": {
    get: { summary: "Read Api" };
  };
  "/api/v1/office/workflows/{workflow_id}/advance": {
    post: { summary: "Advance Api" };
  };
  "/api/v1/office/workflows/{workflow_id}/control": {
    post: { summary: "Control Api" };
  };
  "/api/v1/office/workflows/{workflow_id}/receipts": {
    get: { summary: "History Api" };
  };
  "/api/v1/office/workflows/{workflow_id}/request-approval": {
    post: { summary: "Approval Api" };
  };
  "/api/v1/purchasing/intents": {
    get: { summary: "List Intents Route" };
    post: { summary: "Create Intent Route" };
  };
  "/api/v1/purchasing/intents/{intent_id}": {
    get: { summary: "Get Intent Route" };
  };
  "/api/v1/purchasing/intents/{intent_id}/lines/{line_id}/confirm": {
    post: { summary: "Confirm Line Route" };
  };
  "/api/v1/sales/drafts": {
    get: { summary: "List Drafts Route" };
    post: { summary: "Create Draft Route" };
  };
  "/api/v1/sales/drafts/{draft_id}": {
    get: { summary: "Get Draft Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/handoffs": {
    post: { summary: "Handoff Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/lines": {
    post: { summary: "Add Line Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/lines/{line_id}/confirm": {
    post: { summary: "Confirm Line Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/lines/{line_key}/quote": {
    post: { summary: "Quote Line Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/ready": {
    post: { summary: "Ready Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/void": {
    post: { summary: "Void Route" };
  };
  "/api/v1/sales/handoffs/{handoff_id}/receipt": {
    post: { summary: "Receipt Route" };
  };
  "/api/v1/search": {
    get: { summary: "Search Route" };
  };
  "/api/v1/stocktake/sessions": {
    get: { summary: "List Route" };
    post: { summary: "Create Route" };
  };
  "/api/v1/stocktake/sessions/{session_id}": {
    get: { summary: "Get Route" };
  };
  "/api/v1/stocktake/sessions/{session_id}/blind": {
    get: { summary: "Blind Route" };
  };
  "/api/v1/stocktake/sessions/{session_id}/conversions": {
    post: { summary: "Conversion Route" };
  };
  "/api/v1/stocktake/sessions/{session_id}/observations": {
    get: { summary: "Observations Route" };
    post: { summary: "Append Route" };
  };
  "/api/v1/stocktake/sessions/{session_id}/review-preview": {
    get: { summary: "Review Preview Route" };
  };
  "/api/v1/stocktake/sessions/{session_id}/reviews": {
    post: { summary: "Review Route" };
  };
  "/api/v1/tasks": {
    get: { summary: "List Route" };
    post: { summary: "Create Route" };
  };
  "/api/v1/tasks/managed": {
    post: { summary: "Create Managed Route" };
  };
  "/api/v1/tasks/suggestions": {
    post: { summary: "Create Suggestion Route" };
  };
  "/api/v1/tasks/{task_id}": {
    get: { summary: "Get Route" };
  };
  "/api/v1/tasks/{task_id}/accept": {
    post: { summary: "Accept Route" };
  };
  "/api/v1/tasks/{task_id}/assign": {
    post: { summary: "Assign Route" };
  };
  "/api/v1/tasks/{task_id}/close": {
    post: { summary: "Close Route" };
  };
  "/api/v1/tasks/{task_id}/decision": {
    post: { summary: "Decision Route" };
  };
  "/api/v1/tasks/{task_id}/events": {
    get: { summary: "Events Route" };
  };
  "/api/v1/tasks/{task_id}/start": {
    post: { summary: "Start Route" };
  };
  "/api/v1/tasks/{task_id}/verify": {
    post: { summary: "Verify Route" };
  };
  "/api/v1/workspace/artifacts": {
    get: { summary: "List Artifacts Route" };
    post: { summary: "Create Artifact Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}": {
    get: { summary: "Get Artifact Route" };
    patch: { summary: "Patch Artifact Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}/lock": {
    post: { summary: "Lock Artifact Fields Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}/submit": {
    post: { summary: "Submit Artifact Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}/suggestions/{op_id}/accept": {
    post: { summary: "Accept Suggestion Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}/suggestions/{op_id}/ignore": {
    post: { summary: "Ignore Suggestion Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}/undo": {
    post: { summary: "Undo Artifact Op Route" };
  };
  "/api/v1/workspace/artifacts/{artifact_id}/unlock": {
    post: { summary: "Unlock Artifact Field Route" };
  };
  "/api/v1/workspace/blocks/pin": {
    post: { summary: "Pin Block Route" };
  };
  "/api/v1/workspace/blocks/resolve": {
    post: { summary: "Resolve Blocks Route" };
  };
  "/api/v1/workspace/threads/{thread_id}": {
    get: { summary: "Get Thread Workspace Route" };
    put: { summary: "Upsert Thread Workspace Route" };
  };
  "/api/v1/workspace/threads/{thread_id}/events": {
    get: { summary: "List Thread Events Route" };
  };
  "/health": {
    get: { summary: "Health" };
  };
  "/health/business": {
    get: { summary: "Business Health" };
  };
  "/health/live": {
    get: { summary: "Live" };
  };
  "/health/ready": {
    get: { summary: "Ready" };
  };
  "/api/v1/agent-runs/pi-memory": {
    post: { summary: "Pi Memory Create Route" };
  };
  "/api/v1/agent-runs/pi-readonly": {
    post: { summary: "Pi Create Route" };
  };
  "/api/v1/agent-runs/pi-trial-info": {
    get: { summary: "Pi Trial Info Route" };
  };
  "/api/v1/agent-runs/sdk-readonly": {
    post: { summary: "Sdk Create Route" };
  };
  "/api/v1/agent-runs/{run_id}/runtime-receipt": {
    get: { summary: "Runtime Receipt Route" };
  };
  "/api/v1/agent-runs/{run_id}/tool-results/{tool_step_id}": {
    get: { summary: "Tool Result Route" };
  };
  "/api/v1/assistant/work": {
    get: { summary: "List Route" };
    post: { summary: "Submit" };
  };
  "/api/v1/assistant/work/by-request/{request_id}": {
    get: { summary: "Read Request Route" };
  };
  "/api/v1/assistant/work/intake-status": {
    get: { summary: "Intake Status" };
  };
  "/api/v1/assistant/work/{work_id}": {
    get: { summary: "Read Route" };
  };
  "/api/v1/assistant/work/{work_id}/conversation": {
    get: { summary: "Conversation" };
  };
  "/api/v1/assistant/work/{work_id}/inbox": {
    post: { summary: "Receive Message" };
  };
  "/api/v1/assistant/work/{work_id}/inbox/by-request/{request_id}": {
    get: { summary: "Recover Message" };
  };
  "/api/v1/assistant/work/{work_id}/inbox/{message_id}": {
    get: { summary: "Get Message" };
  };
  "/api/v1/assistant/work/{work_id}/inbox/{message_id}/stop": {
    post: { summary: "Stop Message" };
  };
  "/api/v1/assistant/work/{work_id}/stop": {
    post: { summary: "Stop Route" };
  };
  "/api/v1/assistant/work/{work_id}/turns": {
    get: { summary: "List Followups" };
    post: { summary: "Submit Followup" };
  };
  "/api/v1/assistant/work/{work_id}/turns/by-request/{request_id}": {
    get: { summary: "Recover Followup" };
  };
  "/api/v1/assistant/work/{work_id}/turns/{turn_id}": {
    get: { summary: "Read Followup" };
  };
  "/api/v1/assistant/work/{work_id}/turns/{turn_id}/stop": {
    post: { summary: "Stop Followup" };
  };
  "/api/v1/memory/commands/{operation}/{key}": {
    get: { summary: "Command Status Route" };
  };
  "/api/v1/memory/notes/import": {
    post: { summary: "Import Note Route" };
  };
  "/api/v1/memory/search": {
    get: { summary: "Search Route" };
  };
  "/api/v1/memory/{memory_id}/note": {
    get: { summary: "Export Note Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/line-commands-v2": {
    get: { summary: "Get Line Command V2 Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/line-quotes-v2": {
    get: { summary: "Get Quote V2 Route" };
    post: { summary: "Quote Line V2 Route" };
  };
  "/api/v1/sales/drafts/{draft_id}/lines-v2": {
    post: { summary: "Bind Line V2 Route" };
  };
  "/api/v1/sales/quote-context-v2": {
    get: { summary: "Quote Context V2 Route" };
  };
}
