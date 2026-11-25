import { captureSessionScope, getScopeVersion } from "../lib/auth/session";

export function createArtifactMutationQueue() {
  const queues = new Map<string, Promise<void>>();
  return (artifactId: string, task: (isCurrent: () => boolean) => Promise<void>): Promise<void> => {
    const isCurrent = captureSessionScope();
    const key = String(getScopeVersion()) + ":" + artifactId;
    const previous = queues.get(key) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(async () => {
      if (!isCurrent()) return;
      await task(isCurrent);
    });
    queues.set(key, next);
    const clean = () => { if (queues.get(key) === next) queues.delete(key); };
    void next.then(clean, clean);
    return next;
  };
}

export function acknowledgeDraftInput(
  drafts: Record<string, string>, field: string, sentValue: string,
): Record<string, string> {
  if (drafts[field] !== sentValue) return drafts;
  const next = { ...drafts };
  delete next[field];
  return next;
}


/** Instance-local generation, independent from the identity-wide serial queue. */
export function createArtifactContextFence() {
  let key: string | null = null;
  let generation = 0;
  let active = false;
  return {
    enter(nextKey: string): void {
      if (nextKey !== key) { key = nextKey; generation += 1; active = true; }
    },
    activate(): void { active = true; },
    invalidate(): void { active = false; generation += 1; },
    capture(expectedKey: string): () => boolean {
      const startedGeneration = generation;
      return () => active && key === expectedKey && generation === startedGeneration;
    },
  };
}
