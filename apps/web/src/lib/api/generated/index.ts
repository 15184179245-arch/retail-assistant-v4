import type {
  ArtifactOpRecord,
  ArtifactRecord,
  PendingSuggestionRecord,
  ThreadWorkspaceRecord,
} from "./openapi-types";

export * from "./openapi-types";

export type ArtifactKind = ArtifactRecord["kind"];
export type ArtifactStatus = ArtifactRecord["status"] | "unverified";
export type ArtifactOpDto = ArtifactOpRecord;
export type PendingSuggestionDto = PendingSuggestionRecord;

export interface OpenObjectRefDto {
  kind: string;
  id: string;
  title?: string;
  path?: string;
  artifact_id?: string | null;
}

export interface WorkspaceArtifactDto extends Omit<ArtifactRecord, "status"> {
  status: ArtifactStatus;
}

export interface ThreadWorkspaceDto
  extends Omit<
    ThreadWorkspaceRecord,
    "open_objects" | "active_object_ref" | "version"
  > {
  open_objects: OpenObjectRefDto[];
  active_object_ref: OpenObjectRefDto | null;
  version: number;
  recent_other_editor?: {
    subject_id: string;
    device?: string;
    updated_at: string;
  } | null;
}

export type UiBlockType =
  | "product_card"
  | "product_list"
  | "compare_table"
  | "quote_draft"
  | "purchase_draft"
  | "stock_location"
  | "work_item_list"
  | "evidence_panel"
  | "action_card"
  | "form_request"
  | "artifact_link";

export interface UiBlockRefDto {
  kind: string;
  id: string;
  version?: string | number | null;
  field?: string;
  label?: string;
}

export interface UiBlockDto {
  type: UiBlockType | string;
  version: string | number;
  props: Record<string, unknown>;
  refs: UiBlockRefDto[];
  resolved_refs?: Record<string, unknown> | Array<Record<string, unknown>>;
  resolved_data?: Record<string, unknown>;
  degraded?: boolean;
  fallback_text?: string | null;
  reason?: string | null;
  provenance?: {
    source?: string;
    synced_at?: string;
    verified_by?: string | null;
    status_kind?: string;
  } | null;
}
