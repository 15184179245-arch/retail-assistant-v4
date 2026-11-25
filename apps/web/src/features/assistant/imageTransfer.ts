/** Only event-provided files. Never read the system clipboard or fetch pasted URLs. */
type FileTransfer = Pick<DataTransfer, 'files' | 'items' | 'types'>;

export function transferHasFiles(transfer: FileTransfer): boolean {
  return Array.from(transfer.types).includes('Files') ||
    Array.from(transfer.items).some(item => item.kind === 'file') || transfer.files.length > 0;
}

export function transferFiles(transfer: FileTransfer): File[] {
  const files = Array.from(transfer.files);
  // Some clipboard implementations expose only items. Do not concatenate both
  // representations: they normally describe the same user-selected files.
  return files.length ? files : Array.from(transfer.items)
    .filter(item => item.kind === 'file').map(item => item.getAsFile())
    .filter((file): file is File => file !== null);
}
