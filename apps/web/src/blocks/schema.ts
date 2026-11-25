import type { StatusKind } from '../components/StatusBadge';

/**
 * Runtime schema validator matching contracts/ui-blocks.schema.json v1.
 * Provides zod-compatible `safeParse` semantics with zero external npm dependencies.
 */

export const UI_BLOCK_WHITELIST = [
  'product_card',
  'product_list',
  'compare_table',
  'quote_draft',
  'purchase_draft',
  'stock_location',
  'work_item_list',
  'evidence_panel',
  'action_card',
  'form_request',
  'artifact_link',
] as const;

export type UiBlockType = (typeof UI_BLOCK_WHITELIST)[number];

export const BLOCK_REF_KINDS = [
  'product',
  'price',
  'quote',
  'purchase',
  'stock',
  'location',
  'work_item',
  'evidence',
  'artifact',
  'receiving',
  'stocktake',
  'command',
] as const;

export type BlockRefKind = (typeof BLOCK_REF_KINDS)[number];

export const ARTIFACT_KINDS = [
  'quote_draft',
  'purchase_draft',
  'stocktake_review',
  'receiving_batch',
  'work_item_sheet',
] as const;

export type BlockArtifactKind = (typeof ARTIFACT_KINDS)[number];

const REFS_REQUIRED_TYPES = new Set<UiBlockType>([
  'product_card',
  'product_list',
  'compare_table',
  'quote_draft',
  'purchase_draft',
  'stock_location',
  'work_item_list',
  'evidence_panel',
  'artifact_link',
]);

export const FORBIDDEN_MODEL_PROP_NAMES = new Set<string>([
  'price',
  'unit_price',
  'cost_price',
  'sell_price',
  'list_price',
  'final_price',
  'stock',
  'stock_qty',
  'stock_quantity',
  'on_hand',
  'available_qty',
  'amount',
  'total_amount',
  'subtotal',
  'discount_amount',
  'balance',
  'cost_amount',
  'html',
  'raw_html',
  'inner_html',
  'outer_html',
  'dangerouslySetInnerHTML',
  'dangerously_set_inner_html',
  'script',
  'js',
  'javascript',
  'markup',
  'onclick',
  'onload',
  'onerror',
]);

const UNSAFE_HTML_OR_SCRIPT_PATTERN =
  /<[a-zA-Z!/]|\bon(click|load|error|mouseover|focus|submit)[ \t]*=|javascript[ \t]*:/i;

const VERSION_PATTERN = /^v?1(?:\.[0-9]+)*$/;
const FORM_FIELD_NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

export interface BlockRef {
  kind: BlockRefKind;
  id: string;
  version?: string | number;
  field?: string;
  label?: string;
}

export interface BlockLineRef {
  ref_id: string;
  qty: number;
  unit?: string;
  note?: string;
}

export interface FormFieldSpec {
  name: string;
  label: string;
  field_type: 'text' | 'number' | 'select' | 'boolean' | 'date';
  required?: boolean;
  options?: string[];
  placeholder?: string;
}

export interface BlockProvenance {
  source: string;
  synced_at?: string | null;
  verified_by?: string | null;
  status_kind?: StatusKind;
  stale_refs?: Array<Record<string, unknown>>;
  stripped_fields?: string[];
}

export interface ResolvedRefEntry {
  id?: string;
  kind?: string;
  name?: string;
  sku?: string;
  spec?: string;
  unit?: string;
  location?: string;
  price?: number | string | null;
  unit_price?: number | string | null;
  stock?: number | string | null;
  stock_qty?: number | string | null;
  amount?: number | string | null;
  source?: string;
  synced_at?: string | null;
  observed_at?: string | null;
  verified?: boolean;
  stale?: boolean;
  status_kind?: StatusKind;
  [key: string]: unknown;
}

export interface ValidatedUiBlock {
  type: UiBlockType;
  version: string;
  props: Record<string, unknown>;
  refs: BlockRef[];
  resolved_refs?: Record<string, ResolvedRefEntry> | ResolvedRefEntry[];
  resolved_data?: Record<string, unknown>;
  provenance?: BlockProvenance;
  status_kind?: StatusKind;
  degraded?: boolean;
  fallback_text?: string | null;
  reason?: string | null;
  streaming?: boolean;
}

export interface UiBlockValidationIssue {
  path: string;
  code: string;
  message: string;
}

export type SafeParseResult<T> =
  | { success: true; data: T }
  | {
      success: false;
      error: {
        code: string;
        message: string;
        issues: UiBlockValidationIssue[];
        fallbackText: string;
      };
    };

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isSupportedBlockVersion(version: unknown): boolean {
  if (typeof version === 'number') {
    return version === 1;
  }
  if (typeof version === 'string') {
    return VERSION_PATTERN.test(version.trim());
  }
  return false;
}

export function isSafeText(value: unknown): value is string {
  if (typeof value !== 'string') {
    return false;
  }
  if (value.length > 2000) {
    return false;
  }
  return !UNSAFE_HTML_OR_SCRIPT_PATTERN.test(value);
}

function checkAllowedKeys(
  obj: Record<string, unknown>,
  allowed: readonly string[],
  path: string,
  issues: UiBlockValidationIssue[],
): void {
  const allowedSet = new Set(allowed);
  for (const key of Object.keys(obj)) {
    if (FORBIDDEN_MODEL_PROP_NAMES.has(key) || FORBIDDEN_MODEL_PROP_NAMES.has(key.toLowerCase())) {
      issues.push({
        path: `${path}.${key}`,
        code: 'FORBIDDEN_MODEL_FIELD',
        message: `禁止模型在 props 中直接填写敏感或可执行字段 '${key}'。`,
      });
      continue;
    }
    if (!allowedSet.has(key)) {
      issues.push({
        path: `${path}.${key}`,
        code: 'UNKNOWN_PROP',
        message: `不支持的属性字段 '${key}'。`,
      });
    }
  }
}

function scanDeepForbiddenAndUnsafe(
  value: unknown,
  path: string,
  issues: UiBlockValidationIssue[],
): void {
  if (typeof value === 'string') {
    if (value.length > 2000) {
      issues.push({
        path,
        code: 'TEXT_TOO_LONG',
        message: '文本字段超过 2000 字符上限。',
      });
    } else if (UNSAFE_HTML_OR_SCRIPT_PATTERN.test(value)) {
      issues.push({
        path,
        code: 'UNSAFE_HTML_OR_JS',
        message: '检测到潜在 HTML/脚本注入内容，已拦截。',
      });
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, idx) =>
      scanDeepForbiddenAndUnsafe(item, `${path}[${idx}]`, issues),
    );
    return;
  }
  if (isPlainObject(value)) {
    for (const [k, v] of Object.entries(value)) {
      if (
        FORBIDDEN_MODEL_PROP_NAMES.has(k) ||
        FORBIDDEN_MODEL_PROP_NAMES.has(k.toLowerCase())
      ) {
        issues.push({
          path: `${path}.${k}`,
          code: 'FORBIDDEN_MODEL_FIELD',
          message: `禁止模型在 props 中直接填写 '${k}'（价格/库存/金额/HTML 必须由服务端解析或禁止）。`,
        });
      }
      scanDeepForbiddenAndUnsafe(v, `${path}.${k}`, issues);
    }
  }
}

function validateSafeTextArray(
  value: unknown,
  path: string,
  issues: UiBlockValidationIssue[],
  minItems = 0,
): void {
  if (!Array.isArray(value)) {
    issues.push({ path, code: 'INVALID_TYPE', message: `${path} 必须为数组。` });
    return;
  }
  if (value.length < minItems) {
    issues.push({
      path,
      code: 'TOO_FEW_ITEMS',
      message: `${path} 至少需要 ${minItems} 项。`,
    });
  }
  value.forEach((item, idx) => {
    if (!isSafeText(item)) {
      issues.push({
        path: `${path}[${idx}]`,
        code: 'INVALID_SAFE_TEXT',
        message: `${path}[${idx}] 必须为安全纯文本。`,
      });
    }
  });
}

function validateLineRefs(
  value: unknown,
  path: string,
  issues: UiBlockValidationIssue[],
): void {
  if (!Array.isArray(value)) {
    issues.push({ path, code: 'INVALID_TYPE', message: `${path} 必须为数组。` });
    return;
  }
  value.forEach((item, idx) => {
    const itemPath = `${path}[${idx}]`;
    if (!isPlainObject(item)) {
      issues.push({
        path: itemPath,
        code: 'INVALID_TYPE',
        message: `${itemPath} 必须为对象。`,
      });
      return;
    }
    checkAllowedKeys(item, ['ref_id', 'qty', 'unit', 'note'], itemPath, issues);
    if (
      typeof item.ref_id !== 'string' ||
      item.ref_id.length < 1 ||
      item.ref_id.length > 200
    ) {
      issues.push({
        path: `${itemPath}.ref_id`,
        code: 'INVALID_REF_ID',
        message: 'line_refs.ref_id 必须为非空字符串。',
      });
    }
    if (typeof item.qty !== 'number' || !Number.isFinite(item.qty) || item.qty <= 0) {
      issues.push({
        path: `${itemPath}.qty`,
        code: 'INVALID_QTY',
        message: 'line_refs.qty 必须为大于 0 的数字。',
      });
    }
    if (item.unit !== undefined && !isSafeText(item.unit)) {
      issues.push({
        path: `${itemPath}.unit`,
        code: 'INVALID_SAFE_TEXT',
        message: 'line_refs.unit 必须为安全纯文本。',
      });
    }
    if (item.note !== undefined && !isSafeText(item.note)) {
      issues.push({
        path: `${itemPath}.note`,
        code: 'INVALID_SAFE_TEXT',
        message: 'line_refs.note 必须为安全纯文本。',
      });
    }
  });
}

function validatePropsForType(
  type: UiBlockType,
  props: Record<string, unknown>,
  issues: UiBlockValidationIssue[],
): void {
  scanDeepForbiddenAndUnsafe(props, 'props', issues);

  switch (type) {
    case 'product_card': {
      checkAllowedKeys(
        props,
        ['title', 'highlight', 'compact', 'display_fields'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (props.highlight !== undefined && !isSafeText(props.highlight)) {
        issues.push({ path: 'props.highlight', code: 'INVALID_SAFE_TEXT', message: 'highlight 无效。' });
      }
      if (props.compact !== undefined && typeof props.compact !== 'boolean') {
        issues.push({ path: 'props.compact', code: 'INVALID_TYPE', message: 'compact 必须为布尔值。' });
      }
      if (props.display_fields !== undefined) {
        validateSafeTextArray(props.display_fields, 'props.display_fields', issues);
      }
      break;
    }

    case 'product_list': {
      checkAllowedKeys(
        props,
        ['title', 'empty_text', 'max_items', 'display_fields'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (props.empty_text !== undefined && !isSafeText(props.empty_text)) {
        issues.push({ path: 'props.empty_text', code: 'INVALID_SAFE_TEXT', message: 'empty_text 无效。' });
      }
      if (
        props.max_items !== undefined &&
        (typeof props.max_items !== 'number' ||
          !Number.isInteger(props.max_items) ||
          props.max_items < 1 ||
          props.max_items > 100)
      ) {
        issues.push({ path: 'props.max_items', code: 'INVALID_RANGE', message: 'max_items 须为 1..100 整数。' });
      }
      if (props.display_fields !== undefined) {
        validateSafeTextArray(props.display_fields, 'props.display_fields', issues);
      }
      break;
    }

    case 'compare_table': {
      checkAllowedKeys(
        props,
        ['title', 'columns', 'highlight_ref_id'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      validateSafeTextArray(props.columns, 'props.columns', issues, 1);
      if (
        props.highlight_ref_id !== undefined &&
        (typeof props.highlight_ref_id !== 'string' ||
          props.highlight_ref_id.length > 200)
      ) {
        issues.push({ path: 'props.highlight_ref_id', code: 'INVALID_TYPE', message: 'highlight_ref_id 无效。' });
      }
      break;
    }

    case 'quote_draft': {
      checkAllowedKeys(
        props,
        ['title', 'artifact_id', 'customer_label', 'stage', 'status', 'note', 'line_refs'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (
        props.artifact_id !== undefined &&
        (typeof props.artifact_id !== 'string' || props.artifact_id.length > 100)
      ) {
        issues.push({ path: 'props.artifact_id', code: 'INVALID_TYPE', message: 'artifact_id 无效。' });
      }
      if (props.customer_label !== undefined && !isSafeText(props.customer_label)) {
        issues.push({ path: 'props.customer_label', code: 'INVALID_SAFE_TEXT', message: 'customer_label 无效。' });
      }
      if (props.stage !== undefined && props.stage !== 'draft' && props.stage !== 'ready') {
        issues.push({ path: 'props.stage', code: 'INVALID_ENUM', message: 'quote_draft stage 仅允许 draft | ready。' });
      }
      if (props.status !== undefined && props.status !== 'pending' && props.status !== 'paid') {
        issues.push({ path: 'props.status', code: 'INVALID_ENUM', message: 'quote_draft status 仅允许 pending | paid。' });
      }
      if (props.note !== undefined && !isSafeText(props.note)) {
        issues.push({ path: 'props.note', code: 'INVALID_SAFE_TEXT', message: 'note 无效。' });
      }
      if (props.line_refs !== undefined) {
        validateLineRefs(props.line_refs, 'props.line_refs', issues);
      }
      break;
    }

    case 'purchase_draft': {
      checkAllowedKeys(
        props,
        ['title', 'artifact_id', 'supplier_label', 'note', 'line_refs'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (
        props.artifact_id !== undefined &&
        (typeof props.artifact_id !== 'string' || props.artifact_id.length > 100)
      ) {
        issues.push({ path: 'props.artifact_id', code: 'INVALID_TYPE', message: 'artifact_id 无效。' });
      }
      if (props.supplier_label !== undefined && !isSafeText(props.supplier_label)) {
        issues.push({ path: 'props.supplier_label', code: 'INVALID_SAFE_TEXT', message: 'supplier_label 无效。' });
      }
      if (props.note !== undefined && !isSafeText(props.note)) {
        issues.push({ path: 'props.note', code: 'INVALID_SAFE_TEXT', message: 'note 无效。' });
      }
      if (props.line_refs !== undefined) {
        validateLineRefs(props.line_refs, 'props.line_refs', issues);
      }
      break;
    }

    case 'stock_location': {
      checkAllowedKeys(
        props,
        ['title', 'filter_zone', 'show_siblings'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (props.filter_zone !== undefined && !isSafeText(props.filter_zone)) {
        issues.push({ path: 'props.filter_zone', code: 'INVALID_SAFE_TEXT', message: 'filter_zone 无效。' });
      }
      if (props.show_siblings !== undefined && typeof props.show_siblings !== 'boolean') {
        issues.push({ path: 'props.show_siblings', code: 'INVALID_TYPE', message: 'show_siblings 必须为布尔值。' });
      }
      break;
    }

    case 'work_item_list': {
      checkAllowedKeys(
        props,
        ['title', 'status_filter', 'max_items'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (props.status_filter !== undefined && !isSafeText(props.status_filter)) {
        issues.push({ path: 'props.status_filter', code: 'INVALID_SAFE_TEXT', message: 'status_filter 无效。' });
      }
      if (
        props.max_items !== undefined &&
        (typeof props.max_items !== 'number' ||
          !Number.isInteger(props.max_items) ||
          props.max_items < 1 ||
          props.max_items > 100)
      ) {
        issues.push({ path: 'props.max_items', code: 'INVALID_RANGE', message: 'max_items 须为 1..100 整数。' });
      }
      break;
    }

    case 'evidence_panel': {
      checkAllowedKeys(
        props,
        ['title', 'summary', 'show_provenance'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (props.summary !== undefined && !isSafeText(props.summary)) {
        issues.push({ path: 'props.summary', code: 'INVALID_SAFE_TEXT', message: 'summary 无效。' });
      }
      if (props.show_provenance !== undefined && typeof props.show_provenance !== 'boolean') {
        issues.push({ path: 'props.show_provenance', code: 'INVALID_TYPE', message: 'show_provenance 必须为布尔值。' });
      }
      break;
    }

    case 'action_card': {
      checkAllowedKeys(
        props,
        ['title', 'description', 'action_key', 'confirm_required', 'target_artifact_id', 'parameters'],
        'props',
        issues,
      );
      if (!isSafeText(props.title) || props.title.trim().length === 0) {
        issues.push({ path: 'props.title', code: 'REQUIRED', message: 'action_card 缺少安全 title。' });
      }
      if (
        typeof props.action_key !== 'string' ||
        props.action_key.trim().length === 0 ||
        props.action_key.length > 120
      ) {
        issues.push({ path: 'props.action_key', code: 'REQUIRED', message: 'action_card 缺少合法 action_key。' });
      }
      if (props.description !== undefined && !isSafeText(props.description)) {
        issues.push({ path: 'props.description', code: 'INVALID_SAFE_TEXT', message: 'description 无效。' });
      }
      if (props.confirm_required !== undefined && typeof props.confirm_required !== 'boolean') {
        issues.push({ path: 'props.confirm_required', code: 'INVALID_TYPE', message: 'confirm_required 必须为布尔值。' });
      }
      if (
        props.target_artifact_id !== undefined &&
        (typeof props.target_artifact_id !== 'string' || props.target_artifact_id.length > 100)
      ) {
        issues.push({ path: 'props.target_artifact_id', code: 'INVALID_TYPE', message: 'target_artifact_id 无效。' });
      }
      if (props.parameters !== undefined && !isPlainObject(props.parameters)) {
        issues.push({ path: 'props.parameters', code: 'INVALID_TYPE', message: 'parameters 必须为对象。' });
      }
      break;
    }

    case 'form_request': {
      checkAllowedKeys(
        props,
        ['title', 'form_id', 'submit_label', 'fields'],
        'props',
        issues,
      );
      if (!isSafeText(props.title) || props.title.trim().length === 0) {
        issues.push({ path: 'props.title', code: 'REQUIRED', message: 'form_request 缺少安全 title。' });
      }
      if (
        typeof props.form_id !== 'string' ||
        props.form_id.trim().length === 0 ||
        props.form_id.length > 100
      ) {
        issues.push({ path: 'props.form_id', code: 'REQUIRED', message: 'form_request 缺少合法 form_id。' });
      }
      if (props.submit_label !== undefined && !isSafeText(props.submit_label)) {
        issues.push({ path: 'props.submit_label', code: 'INVALID_SAFE_TEXT', message: 'submit_label 无效。' });
      }
      if (!Array.isArray(props.fields) || props.fields.length < 1) {
        issues.push({ path: 'props.fields', code: 'REQUIRED', message: 'form_request 至少包含 1 个字段定义。' });
      } else {
        props.fields.forEach((field, idx) => {
          const fPath = `props.fields[${idx}]`;
          if (!isPlainObject(field)) {
            issues.push({ path: fPath, code: 'INVALID_TYPE', message: '字段定义必须为对象。' });
            return;
          }
          checkAllowedKeys(
            field,
            ['name', 'label', 'field_type', 'required', 'options', 'placeholder'],
            fPath,
            issues,
          );
          if (
            typeof field.name !== 'string' ||
            field.name.length < 1 ||
            field.name.length > 80 ||
            !FORM_FIELD_NAME_PATTERN.test(field.name)
          ) {
            issues.push({ path: `${fPath}.name`, code: 'INVALID_FIELD_NAME', message: '字段名格式非法。' });
          }
          if (!isSafeText(field.label) || field.label.trim().length === 0) {
            issues.push({ path: `${fPath}.label`, code: 'REQUIRED', message: '字段 label 无效。' });
          }
          if (
            !['text', 'number', 'select', 'boolean', 'date'].includes(
              String(field.field_type),
            )
          ) {
            issues.push({ path: `${fPath}.field_type`, code: 'INVALID_ENUM', message: 'field_type 不受支持。' });
          }
          if (field.required !== undefined && typeof field.required !== 'boolean') {
            issues.push({ path: `${fPath}.required`, code: 'INVALID_TYPE', message: 'required 必须为布尔值。' });
          }
          if (field.options !== undefined) {
            validateSafeTextArray(field.options, `${fPath}.options`, issues);
          }
          if (field.placeholder !== undefined && !isSafeText(field.placeholder)) {
            issues.push({ path: `${fPath}.placeholder`, code: 'INVALID_SAFE_TEXT', message: 'placeholder 无效。' });
          }
        });
      }
      break;
    }

    case 'artifact_link': {
      checkAllowedKeys(
        props,
        ['title', 'artifact_id', 'artifact_kind', 'action_label'],
        'props',
        issues,
      );
      if (props.title !== undefined && !isSafeText(props.title)) {
        issues.push({ path: 'props.title', code: 'INVALID_SAFE_TEXT', message: 'title 无效。' });
      }
      if (
        typeof props.artifact_id !== 'string' ||
        props.artifact_id.trim().length === 0 ||
        props.artifact_id.length > 100
      ) {
        issues.push({ path: 'props.artifact_id', code: 'REQUIRED', message: 'artifact_link 缺少 artifact_id。' });
      }
      if (!ARTIFACT_KINDS.includes(props.artifact_kind as BlockArtifactKind)) {
        issues.push({ path: 'props.artifact_kind', code: 'INVALID_ENUM', message: 'artifact_kind 不受支持。' });
      }
      if (props.action_label !== undefined && !isSafeText(props.action_label)) {
        issues.push({ path: 'props.action_label', code: 'INVALID_SAFE_TEXT', message: 'action_label 无效。' });
      }
      break;
    }
  }
}

function buildFallbackText(input: unknown): string {
  if (isPlainObject(input)) {
    if (typeof input.fallback_text === 'string' && input.fallback_text.trim().length > 0) {
      return input.fallback_text;
    }
    const typeLabel = typeof input.type === 'string' ? input.type : 'unknown';
    const titleVal =
      isPlainObject(input.props) && typeof input.props.title === 'string'
        ? input.props.title
        : '';
    return titleVal
      ? `[块已安全降级: ${typeLabel}] ${titleVal}`
      : `[块已安全降级: ${typeLabel}]`;
  }
  return '[块载荷格式非法，已安全降级为纯文本]';
}

function hasServerVerifiedResolution(record: Record<string, unknown>): boolean {
  const resolvedRefs = record.resolved_refs;
  const resolvedData = record.resolved_data;
  if (isPlainObject(resolvedRefs) && Object.keys(resolvedRefs).length > 0) {
    return Object.values(resolvedRefs).every(
      (entry) => !isPlainObject(entry) || entry.verified !== false,
    );
  }
  if (Array.isArray(resolvedRefs) && resolvedRefs.length > 0) {
    return resolvedRefs.every(
      (entry) => !isPlainObject(entry) || entry.verified !== false,
    );
  }
  if (isPlainObject(resolvedData) && Object.keys(resolvedData).length > 0) {
    return record.status_kind !== 'unverified';
  }
  return false;
}

/**
 * Runtime schema validator matching `contracts/ui-blocks.schema.json` v1 with zod-style `safeParse` semantics.
 */
export function validateUiBlock(input: unknown): SafeParseResult<ValidatedUiBlock> {
  const fallbackText = buildFallbackText(input);
  if (!isPlainObject(input)) {
    return {
      success: false,
      error: {
        code: 'INVALID_BLOCK_ENVELOPE',
        message: '生成式块载荷必须为 JSON 对象。',
        issues: [{ path: '$', code: 'INVALID_TYPE', message: '必须为对象' }],
        fallbackText,
      },
    };
  }

  if (input.degraded === true) {
    return {
      success: false,
      error: {
        code: 'SERVER_DEGRADED',
        message:
          typeof input.reason === 'string' && input.reason.trim().length > 0
            ? input.reason
            : '服务端已拒绝或降级此生成式块。',
        issues: [{ path: 'degraded', code: 'SERVER_DEGRADED', message: '服务端降级' }],
        fallbackText,
      },
    };
  }

  const issues: UiBlockValidationIssue[] = [];

  if (
    typeof input.type !== 'string' ||
    !UI_BLOCK_WHITELIST.includes(input.type as UiBlockType)
  ) {
    issues.push({
      path: 'type',
      code: 'UNKNOWN_BLOCK_TYPE',
      message: `未知或未列入 v1 白名单的块类型: '${String(input.type)}'。`,
    });
  }

  if (!isSupportedBlockVersion(input.version)) {
    issues.push({
      path: 'version',
      code: 'UNSUPPORTED_VERSION',
      message: `不支持的块协议版本: '${String(input.version)}'（仅支持 1 / 1.x / v1）。`,
    });
  }

  if (!isPlainObject(input.props)) {
    issues.push({
      path: 'props',
      code: 'INVALID_PROPS',
      message: 'props 必须为 JSON 对象。',
    });
  }

  if (!Array.isArray(input.refs)) {
    issues.push({
      path: 'refs',
      code: 'MISSING_REFS',
      message: '缺少必需的 refs 数组。',
    });
  } else {
    input.refs.forEach((ref, idx) => {
      const rPath = `refs[${idx}]`;
      if (!isPlainObject(ref)) {
        issues.push({ path: rPath, code: 'INVALID_REF', message: 'ref 必须为对象。' });
        return;
      }
      const allowedRefKeys = new Set(['kind', 'id', 'version', 'field', 'label']);
      for (const k of Object.keys(ref)) {
        if (!allowedRefKeys.has(k)) {
          issues.push({ path: `${rPath}.${k}`, code: 'UNKNOWN_REF_FIELD', message: `ref 含未知字段 '${k}'` });
        }
      }
      if (
        typeof ref.kind !== 'string' ||
        !BLOCK_REF_KINDS.includes(ref.kind as BlockRefKind)
      ) {
        issues.push({ path: `${rPath}.kind`, code: 'INVALID_REF_KIND', message: 'ref.kind 不合法。' });
      }
      if (typeof ref.id !== 'string' || ref.id.trim().length === 0 || ref.id.length > 200) {
        issues.push({ path: `${rPath}.id`, code: 'INVALID_REF_ID', message: 'ref.id 必须为非空字符串。' });
      }
      if (
        ref.version !== undefined &&
        typeof ref.version !== 'string' &&
        typeof ref.version !== 'number'
      ) {
        issues.push({ path: `${rPath}.version`, code: 'INVALID_REF_VERSION', message: 'ref.version 类型错误。' });
      }
      if (
        ref.field !== undefined &&
        (typeof ref.field !== 'string' || ref.field.length > 100)
      ) {
        issues.push({ path: `${rPath}.field`, code: 'INVALID_REF_FIELD', message: 'ref.field 无效。' });
      }
      if (ref.label !== undefined && !isSafeText(ref.label)) {
        issues.push({ path: `${rPath}.label`, code: 'INVALID_SAFE_TEXT', message: 'ref.label 含不安全内容。' });
      }
    });
  }

  const blockType = input.type as UiBlockType;
  if (
    UI_BLOCK_WHITELIST.includes(blockType) &&
    REFS_REQUIRED_TYPES.has(blockType) &&
    Array.isArray(input.refs) &&
    input.refs.length < 1
  ) {
    issues.push({
      path: 'refs',
      code: 'MISSING_REFS',
      message: `${blockType} 必须绑定至少 1 条服务端 refs 引用。`,
    });
  }

  if (UI_BLOCK_WHITELIST.includes(blockType) && isPlainObject(input.props)) {
    validatePropsForType(blockType, input.props, issues);
  }

  // If unverified price/stock/amount fields appear inside resolved_refs without server verification, reject too
  if (input.resolved_refs !== undefined) {
    scanDeepForbiddenAndUnsafe(
      // Strip allowed financial keys inside resolved_refs only when server verified; always block HTML/JS!
      stripAllowedFinancialKeys(input.resolved_refs),
      'resolved_refs',
      issues,
    );
    if (!hasServerVerifiedResolution(input)) {
      const refsArr = Array.isArray(input.resolved_refs)
        ? input.resolved_refs
        : isPlainObject(input.resolved_refs)
          ? Object.values(input.resolved_refs)
          : [];
      const explicitlyUnverified = refsArr.some(
        (item) => isPlainObject(item) && item.verified === false && item.stale !== true,
      );
      if (explicitlyUnverified) {
        issues.push({
          path: 'resolved_refs',
          code: 'UNVERIFIED_RESOLVED_REFS',
          message: '价格、库存或金额引用尚未通过服务端核验，已安全降级。',
        });
      }
    }
  }

  if (issues.length > 0) {
    return {
      success: false,
      error: {
        code: issues[0]?.code ?? 'BLOCK_VALIDATION_FAILED',
        message: issues.map((i) => i.message).join('；'),
        issues,
        fallbackText,
      },
    };
  }

  const normalizedVersion =
    typeof input.version === 'number' ? String(input.version) : String(input.version).trim();

  return {
    success: true,
    data: {
      type: blockType,
      version: normalizedVersion,
      props: input.props as Record<string, unknown>,
      refs: input.refs as BlockRef[],
      resolved_refs: input.resolved_refs as ValidatedUiBlock['resolved_refs'],
      resolved_data: isPlainObject(input.resolved_data)
        ? input.resolved_data
        : undefined,
      provenance: isPlainObject(input.provenance)
        ? (input.provenance as unknown as BlockProvenance)
        : undefined,
      status_kind: typeof input.status_kind === 'string'
        ? (input.status_kind as StatusKind)
        : undefined,
      degraded: false,
      streaming: input.streaming === true,
    },
  };
}

function stripAllowedFinancialKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(stripAllowedFinancialKeys);
  }
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      if (
        ['price', 'unit_price', 'cost_price', 'sell_price', 'list_price', 'final_price', 'stock', 'stock_qty', 'stock_quantity', 'on_hand', 'available_qty', 'amount', 'total_amount', 'subtotal', 'discount_amount', 'balance', 'cost_amount'].includes(k)
      ) {
        // Still validate string values for HTML/script safety without triggering FORBIDDEN_MODEL_FIELD on server resolved_refs!
        if (typeof v === 'string') {
          out[`resolved_${k}`] = v;
        }
        continue;
      }
      out[k] = stripAllowedFinancialKeys(v);
    }
    return out;
  }
  return value;
}

export const uiBlockSchema = {
  safeParse: validateUiBlock,
  parse(input: unknown): ValidatedUiBlock {
    const result = validateUiBlock(input);
    if (!result.success) {
      throw new Error(`${result.error.code}: ${result.error.message}`);
    }
    return result.data;
  },
};
