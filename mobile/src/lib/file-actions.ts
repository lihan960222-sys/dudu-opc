// Saving a workspace file on the phone and handing it to the system share sheet (which also offers Save to Files or Drive).

import { Directory, File, Paths } from 'expo-file-system';
import * as Sharing from 'expo-sharing';
import { randomUUID } from 'expo-crypto';

import { safeFileName } from './files';

export type FileSource = { uri: string; headers: Record<string, string> };

/** Each export has a private cache file so another share cannot overwrite it while it is open. */
export async function shareStewardMarkdown(text: string): Promise<'shared' | 'unavailable'> {
  if (!(await Sharing.isAvailableAsync())) return 'unavailable';
  const dir = new Directory(Paths.cache, 'steward-replies', randomUUID());
  dir.create({ intermediates: true, idempotent: true });
  const file = new File(dir, '完整回复.md');
  await file.write(text);
  await Sharing.shareAsync(file.uri, { mimeType: 'text/markdown', UTI: 'net.daringfireball.markdown', dialogTitle: '保存或分享完整回复' });
  return 'shared';
}

/** Downloads the file into the cache (the previous one is cleared first) and opens the share sheet. */
export async function shareFile(source: FileSource, name: string, mime: string): Promise<'shared' | 'unavailable'> {
  if (!(await Sharing.isAvailableAsync())) return 'unavailable';
  const folder = new Directory(Paths.cache, 'shared-files');
  if (folder.exists) folder.delete();
  folder.create({ intermediates: true, idempotent: true });
  const file = await File.downloadFileAsync(source.uri, new File(folder, safeFileName(name)), { headers: source.headers, idempotent: true });
  await Sharing.shareAsync(file.uri, { mimeType: mime, dialogTitle: name });
  return 'shared';
}
