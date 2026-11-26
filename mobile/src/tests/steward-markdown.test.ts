import { shareStewardMarkdown } from '@/lib/file-actions';

const mockWrite = jest.fn();
const mockCreateDirectory = jest.fn();
const mockShare = jest.fn();
const mockAvailable = jest.fn();
jest.mock('expo-file-system', () => ({
  Paths: { cache: 'private-cache' },
  Directory: class { create = mockCreateDirectory; },
  File: class { uri = 'private-cache/完整回复.md'; write = mockWrite; },
}));
jest.mock('expo-sharing', () => ({
  isAvailableAsync: () => mockAvailable(),
  shareAsync: (...args: unknown[]) => mockShare(...args),
}));
jest.mock('expo-crypto', () => ({
  randomUUID: () => 'export-id',
}));

beforeEach(() => jest.clearAllMocks());

test('shares the complete Markdown only after writing the file finishes', async () => {
  const text = '老板，运行尚未完成。\n```sh\nexit 7\n```\n' + '𠮷'.repeat(1000);
  mockAvailable.mockResolvedValue(true);
  let writing!: () => void;
  const started = new Promise<void>(resolve => { writing = resolve; });
  let written!: () => void;
  mockWrite.mockImplementationOnce(() => {
    writing();
    return new Promise<void>(resolve => { written = resolve; });
  });
  const done = shareStewardMarkdown(text);
  await started;
  expect(mockWrite).toHaveBeenCalledWith(text);
  expect(mockShare).not.toHaveBeenCalled();
  written();
  await expect(done).resolves.toBe('shared');
  expect(mockShare).toHaveBeenCalledWith('private-cache/完整回复.md', expect.objectContaining({ mimeType: 'text/markdown' }));
});

test('creates no cache file when file sharing is unavailable', async () => {
  mockAvailable.mockResolvedValue(false);
  await expect(shareStewardMarkdown('test')).resolves.toBe('unavailable');
  expect(mockCreateDirectory).not.toHaveBeenCalled();
  expect(mockWrite).not.toHaveBeenCalled();
});
