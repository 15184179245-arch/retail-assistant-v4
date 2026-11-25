/**
 * Utilities for Idempotency-Key generation and retry tracking in write operations.
 * Requirements: R-034, R-006.
 */

export function generateIdempotencyKey(prefix = "web"): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}_${crypto.randomUUID()}`;
  }

  // Fallback for older or restricted environments
  const timestamp = Date.now().toString(36);
  const random = Math.random().toString(36).substring(2, 10);
  return `${prefix}_${timestamp}_${random}`;
}

/**
 * Encapsulates an idempotent action attempt that retains its idempotency key across retries.
 */
export class IdempotentScope {
  private key: string;

  constructor(prefix = "web", initialKey?: string) {
    this.key = initialKey ?? generateIdempotencyKey(prefix);
  }

  /**
   * Returns the frozen idempotency key for this operational scope.
   */
  getKey(): string {
    return this.key;
  }

  /**
   * Resets the key ONLY when a completely fresh user action is initiated (not on retry).
   */
  refresh(prefix = "web"): string {
    this.key = generateIdempotencyKey(prefix);
    return this.key;
  }
}
