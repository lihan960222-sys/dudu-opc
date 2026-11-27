// Clean only local Dudu conversation caches. Keep new-chat drafts, settings
// and any other workspace sharing this browser origin.
export function clearConversationCaches(storage: Storage) {
  const historyKey = /^suparagent:(?:(?:draft:|composer:v2:)(?:sending:)?\d+|(?:queued-follow-ups|reactions):\d+|thread-seen)$/;
  for (let i = storage.length - 1; i >= 0; i--) {
    const key = storage.key(i);
    if (key && historyKey.test(key)) storage.removeItem(key);
  }
}
