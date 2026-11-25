import type {Work} from './assistantApi';
/** Version the rendered, already-allowlisted conversation projection, not merely its
 * execution status or answer length. No raw receipts or extra persisted state. */
export function conversationUpdateKey(selectedId:string|null,privacyEpoch:number,timeline:readonly Work[]):string{
  return JSON.stringify([selectedId,privacyEpoch,timeline.map(work=>[
    work.work_id,work.run_id,work.text,work.created_at,work.execution_status,
    work.answer,work.answerUnavailable,work.citations,work.images,work.inbox_intake,work.progress,
  ])]);
}
