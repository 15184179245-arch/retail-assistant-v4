export {
  UI_BLOCK_WHITELIST,
  BLOCK_REF_KINDS,
  ARTIFACT_KINDS,
  FORBIDDEN_MODEL_PROP_NAMES,
  isSupportedBlockVersion,
  isSafeText,
  validateUiBlock,
  uiBlockSchema,
} from './schema';
export type {
  UiBlockType,
  BlockRefKind,
  BlockArtifactKind,
  BlockRef,
  BlockLineRef,
  FormFieldSpec,
  BlockProvenance,
  ResolvedRefEntry,
  ValidatedUiBlock,
  UiBlockValidationIssue,
  SafeParseResult,
} from './schema';

export {
  BlockRenderer,
  BlockSkeleton,
  BlockFallbackCard,
} from './BlockRenderer';
export type { BlockRendererProps } from './BlockRenderer';

export {
  L1CoeditField,
  PartialUndoNotice,
  computePartialUndoPlan,
} from './L1CoeditField';
export type {
  L1CoeditFieldProps,
  PartialUndoNoticeProps,
  PendingAiSuggestion,
  PartialUndoPlan,
} from './L1CoeditField';
