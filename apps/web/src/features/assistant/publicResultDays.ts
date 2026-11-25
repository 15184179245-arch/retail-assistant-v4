import type { ResultTable } from './publicResultSnapshot';

export type DayBand = { day: string; count: number };
/** Match the producer's _bill_day: source calendar date, never UTC conversion. */
export function sourceBusinessDay(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4}-\d{2}-\d{2})(?:[ T](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?)?$/.exec(value);
  if (!match || match[1].startsWith('0000')) return null;
  const day=match[1], time=new Date(day+'T00:00:00Z');
  return Number.isFinite(time.getTime()) && time.toISOString().slice(0,10)===day ? day : null;
}
/** Count only visible source rows; no currency, quantity, unit or complete-report inference. */
export function publicResultDays(block: ResultTable): DayBand[] {
  const index = block.columns.findIndex(c => c.key === 'bill_date');
  if (index < 0) return [];
  const counts = new Map<string, number>();
  for (const {cells} of block.rows) {
    const date=sourceBusinessDay(cells[index]);
    if(!date)return [];
    counts.set(date, (counts.get(date) ?? 0) + 1);
  }
  return Array.from(counts, ([day,count]) => ({ day,count })).sort((a,b) => a.day < b.day ? -1 : 1);
}
